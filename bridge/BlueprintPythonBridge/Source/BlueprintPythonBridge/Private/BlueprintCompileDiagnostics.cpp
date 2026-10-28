// =============================================================================
//  BlueprintCompileDiagnostics.cpp — 结构化编译诊断（FCompilerResultsLog）
//  T-20261027-UE5BRIDGE-COMPILE-STRUCT · v4.6
// =============================================================================
//  动机：日志正则路线的三个先天缺陷
//    ① 依赖日志异步刷盘（毫秒级连续调用会扑空，只能 sleep 2s 重读）；
//    ② 节点名来自日志文本，同名节点 / 本地化标题会让 GUID 反查不可靠；
//    ③ tail 窗口天然含上一轮的历史错误（必须靠 compiled 收敛，errors 不能清空）。
//
//  做法：编译时传入自己的 FCompilerResultsLog，直接读 token 化消息：
//    · EMessageToken::EdGraph → FEdGraphToken: GetGraphObject()/GetPin()
//      → UEdGraphNode（NodeGuid 权威、图名权威）；
//    · EMessageToken::Object  → FUObjectToken: GetObject()
//      → 节点 / 蓝图 / 函数（按类型分派）；
//    · 消息文本 ToText()、严重度 GetSeverity()、计数 NumErrors/NumWarnings。
//  一条消息可关联多个节点 → 顶层给第一个，nodes[] 给全部（含 GUID）。
//
//  与旧路径的关系：
//    · 仍然调用 FKismetEditorUtilities::CompileBlueprint（输出日志照常写，
//      Python 侧 log_matches / 人类排查不受影响）；
//    · Python 侧优先用本函数；旧 DLL 无此入口时自动回退日志正则
//      （见 _compile_bp_and_scan_log）。
//  API 核对：5.1 与 5.8 的 CompileBlueprint 签名 / CompilerResultsLog 字段
//  / FEdGraphToken::GetGraphObject 完全一致（逐头文件比对）。
// =============================================================================

#include "BlueprintPythonBridge.h"

#include "Editor.h"
#include "Engine/Blueprint.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphToken.h"
#include "Misc/UObjectToken.h"
#include "Logging/TokenizedMessage.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Async/Async.h"

namespace
{
    template <typename Func>
    auto CompileDiagOnGameThread(Func&& InFunc) -> decltype(InFunc())
    {
        if (IsInGameThread())
        {
            return InFunc();
        }
        return Async(EAsyncExecution::TaskGraphMainThread,
            std::forward<Func>(InFunc)).Get();
    }

    /** 字符串 → JSON 安全文本（反斜杠 / 引号 / 控制字符）。 */
    FString CompileDiagEscapeJson(const FString& In)
    {
        FString Out;
        Out.Reserve(In.Len() + 8);
        for (const TCHAR Ch : In)
        {
            switch (Ch)
            {
            case TEXT('\\'): Out += TEXT("\\\\"); break;
            case TEXT('"'):  Out += TEXT("\\\""); break;
            case TEXT('\n'): Out += TEXT("\\n"); break;
            case TEXT('\r'): Out += TEXT("\\r"); break;
            case TEXT('\t'): Out += TEXT("\\t"); break;
            default:
                if (Ch < 0x20)
                {
                    Out += FString::Printf(TEXT("\\u%04x"), static_cast<int32>(Ch));
                }
                else
                {
                    Out.AppendChar(Ch);
                }
            }
        }
        return Out;
    }

    FString CompileDiagFailJson(const FString& Message)
    {
        return FString::Printf(TEXT("{\"ok\":false,\"error\":\"%s\"}"),
            *CompileDiagEscapeJson(Message));
    }

    FString SeverityToString(EMessageSeverity::Type Severity)
    {
        switch (Severity)
        {
        case EMessageSeverity::Error:              return TEXT("error");
        case EMessageSeverity::Warning:            return TEXT("warning");
        case EMessageSeverity::PerformanceWarning: return TEXT("performance_warning");
        case EMessageSeverity::Info:
        default:                                   return TEXT("info");
        }
    }

    /** 蓝图 → 包路径（/Game/X/BP_Y，不带 .BP_Y 对象后缀，与日志 [AssetLog] 同口径）。 */
    FString BlueprintPackagePath(const UBlueprint* BP)
    {
        if (!BP)
        {
            return FString();
        }
        const UPackage* Package = BP->GetOutermost();
        return Package ? Package->GetName() : BP->GetPathName();
    }

    /** 节点 → 一行 JSON（GUID 用 Digits 格式，与 Python 侧 export_text 同口径）。 */
    FString NodeToJson(const UEdGraphNode* Node)
    {
        if (!Node)
        {
            return TEXT("null");
        }
        const UEdGraph* Graph = Node->GetGraph();
        const UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForNode(Node);
        const FString Title = Node->GetNodeTitle(ENodeTitleType::ListView).ToString();
        return FString::Printf(
            TEXT("{\"node_guid\":\"%s\",\"node_name\":\"%s\",\"graph\":\"%s\",\"asset\":\"%s\"}"),
            *Node->NodeGuid.ToString(EGuidFormats::Digits),
            *CompileDiagEscapeJson(Title),
            *CompileDiagEscapeJson(Graph ? Graph->GetName() : FString()),
            *CompileDiagEscapeJson(BlueprintPackagePath(BP)));
    }

    /** 把消息 token 里能定位到的节点收进 OutNodes（去重、保持顺序）。 */
    void CollectNodesFromMessage(const FTokenizedMessage& Message,
                                 TArray<const UEdGraphNode*>& OutNodes)
    {
        auto AddNode = [&OutNodes](const UEdGraphNode* Node)
        {
            if (Node && !OutNodes.Contains(Node))
            {
                OutNodes.Add(Node);
            }
        };

        for (const TSharedRef<IMessageToken>& Token : Message.GetMessageTokens())
        {
            if (Token->GetType() == EMessageToken::EdGraph)
            {
                const FEdGraphToken& EdToken =
                    static_cast<const FEdGraphToken&>(Token.Get());
                if (const UEdGraphPin* Pin = EdToken.GetPin())
                {
                    AddNode(Pin->GetOwningNode());
                }
                else if (const UObject* Obj = EdToken.GetGraphObject())
                {
                    if (const UEdGraphNode* Node = Cast<UEdGraphNode>(Obj))
                    {
                        AddNode(Node);
                    }
                }
            }
            else if (Token->GetType() == EMessageToken::Object)
            {
                const FUObjectToken& ObjToken =
                    static_cast<const FUObjectToken&>(Token.Get());
                if (const UObject* Obj = ObjToken.GetObject().Get())
                {
                    if (const UEdGraphNode* Node = Cast<UEdGraphNode>(Obj))
                    {
                        AddNode(Node);
                    }
                }
            }
        }
    }

    /** 一条消息 → JSON 对象（含 nodes[] 与顶层首节点字段）。 */
    FString MessageToJson(const TSharedRef<FTokenizedMessage>& Message,
                          const UBlueprint* FallbackBP)
    {
        const FString Text = Message->ToText().ToString();
        TArray<const UEdGraphNode*> Nodes;
        CollectNodesFromMessage(*Message, Nodes);

        FString NodesJson;
        for (int32 Index = 0; Index < Nodes.Num(); ++Index)
        {
            if (Index > 0)
            {
                NodesJson += TEXT(",");
            }
            NodesJson += NodeToJson(Nodes[Index]);
        }

        // 顶层定位字段：优先节点，其次回退到被编译蓝图
        FString NodeGuid, NodeName, GraphName, AssetPath;
        if (Nodes.Num() > 0)
        {
            const UEdGraphNode* Node = Nodes[0];
            NodeGuid = Node->NodeGuid.ToString(EGuidFormats::Digits);
            GraphName = Node->GetGraph() ? Node->GetGraph()->GetName() : FString();
            NodeName = Node->GetNodeTitle(ENodeTitleType::ListView).ToString();
            const UBlueprint* NodeBP = FBlueprintEditorUtils::FindBlueprintForNode(Node);
            AssetPath = BlueprintPackagePath(NodeBP);
        }
        if (AssetPath.IsEmpty())
        {
            AssetPath = BlueprintPackagePath(FallbackBP);
        }

        return FString::Printf(
            TEXT("{\"severity\":\"%s\",\"message\":\"%s\",")
            TEXT("\"node_guid\":\"%s\",\"node_name\":\"%s\",\"graph\":\"%s\",")
            TEXT("\"asset\":\"%s\",\"nodes\":[%s]}"),
            *SeverityToString(Message->GetSeverity()),
            *CompileDiagEscapeJson(Text),
            *NodeGuid,
            *CompileDiagEscapeJson(NodeName),
            *CompileDiagEscapeJson(GraphName),
            *CompileDiagEscapeJson(AssetPath),
            *NodesJson);
    }

    FString BlueprintStatusToString(EBlueprintStatus Status)
    {
        switch (Status)
        {
        case BS_Unknown:            return TEXT("BS_Unknown");
        case BS_Dirty:              return TEXT("BS_Dirty");
        case BS_Error:              return TEXT("BS_Error");
        case BS_UpToDate:           return TEXT("BS_UpToDate");
        case BS_BeingCreated:       return TEXT("BS_BeingCreated");
        case BS_UpToDateWithWarnings: return TEXT("BS_UpToDateWithWarnings");
        default:                    return TEXT("unknown");
        }
    }
} // namespace

FString UBlueprintPythonBridge::CompileBlueprintStructured(UBlueprint* Blueprint)
{
    return CompileDiagOnGameThread([Blueprint]() -> FString
    {
        if (!Blueprint)
        {
            return CompileDiagFailJson(TEXT("Blueprint 为空"));
        }

        FCompilerResultsLog Results;
        Results.bAnnotateMentionedNodes = true;
        Results.SetSourcePath(Blueprint->GetPathName());

        FKismetEditorUtilities::CompileBlueprint(
            Blueprint, EBlueprintCompileOptions::None, &Results);

        const bool bCompiled =
            (Blueprint->Status == BS_UpToDate
             || Blueprint->Status == BS_Dirty
             || Blueprint->Status == BS_UpToDateWithWarnings);

        FString ErrorsJson, WarningsJson, MessagesJson;
        int32 ErrorCount = 0;
        int32 WarningCount = 0;
        int32 InfoCount = 0;
        for (const TSharedRef<FTokenizedMessage>& Message : Results.Messages)
        {
            const FString ItemJson = MessageToJson(Message, Blueprint);
            const EMessageSeverity::Type Severity = Message->GetSeverity();
            if (Severity == EMessageSeverity::Error)
            {
                ErrorsJson += (ErrorCount > 0 ? TEXT(",") : TEXT("")) + ItemJson;
                ++ErrorCount;
            }
            else if (Severity == EMessageSeverity::Warning
                     || Severity == EMessageSeverity::PerformanceWarning)
            {
                WarningsJson += (WarningCount > 0 ? TEXT(",") : TEXT("")) + ItemJson;
                ++WarningCount;
            }
            else
            {
                MessagesJson += (InfoCount > 0 ? TEXT(",") : TEXT("")) + ItemJson;
                ++InfoCount;
            }
        }

        UE_LOG(LogTemp, Log,
            TEXT("[BPBridge] CompileBlueprintStructured '%s': status=%s compiled=%d "
                 "errors=%d warnings=%d"),
            *Blueprint->GetName(), *BlueprintStatusToString(Blueprint->Status),
            (bCompiled ? 1 : 0), ErrorCount, WarningCount);

        return FString::Printf(
            TEXT("{\"ok\":true,\"compiled\":%s,\"status\":\"%s\",\"status_code\":%d,")
            TEXT("\"asset\":\"%s\",\"num_errors\":%d,\"num_warnings\":%d,")
            TEXT("\"errors\":[%s],\"warnings\":[%s],\"messages\":[%s]}"),
            (bCompiled ? TEXT("true") : TEXT("false")),
            *BlueprintStatusToString(Blueprint->Status),
            static_cast<int32>(Blueprint->Status),
            *CompileDiagEscapeJson(BlueprintPackagePath(Blueprint)),
            ErrorCount, WarningCount,
            *ErrorsJson, *WarningsJson, *MessagesJson);
    });
}

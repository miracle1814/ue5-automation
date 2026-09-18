#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "Templates/SubclassOf.h"
#include "BlueprintPythonBridge.generated.h"

// ---- Node Info ----

USTRUCT(BlueprintType)
struct FBPNodeInfo
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString NodeTitle;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString NodeClass;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") int32 PosX = 0;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") int32 PosY = 0;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FGuid NodeGuid;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString GraphName;
};

USTRUCT(BlueprintType)
struct FBPPinInfo
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString PinName;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString PinDirection;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString PinType;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") bool bIsConnected = false;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") bool bIsExecutionPin = false;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString PinId;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString DefaultValue;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString PinSubCategory;
};

USTRUCT(BlueprintType)
struct FBPConnectionInfo
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString FromNodeTitle;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString FromPinName;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString FromPinDirection;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FGuid FromNodeGuid;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString ToNodeTitle;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString ToPinName;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString ToPinDirection;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FGuid ToNodeGuid;
};

// T-20260823-UE5BRIDGE-CPP-APIS 子项2：蓝图变量清单条目（ReadVariables 返回结构）
USTRUCT(BlueprintType)
struct FBPVariableInfo
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString VariableName;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString VariableType;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString DefaultValue;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString Category;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") bool bIsEditable = false;
};

// v3.0（T-20260918-V3-UMG）：控件树条目（ReadWidgetTree 返回结构）
USTRUCT(BlueprintType)
struct FBPWidgetInfo
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString WidgetName;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString WidgetClass;
    UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge") FString ParentName;
};

// ---- Library ----

UCLASS()
class BLUEPRINTPYTHONBRIDGE_API UBlueprintPythonBridge : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:

    /** 查找 EventGraph（纯查询，不创建） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraph* FindEventGraph(UBlueprint* Blueprint);

    /** 获取或创建 EventGraph（空图，无自动事件） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraph* GetEventGraph(UBlueprint* Blueprint);

    /** 创建空的 EventGraph（已存在则直接返回，不触发 AddUbergraphPage） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraph* CreateEventGraph(UBlueprint* Blueprint);

    /** 用 UE 原生方式创建 Actor 蓝图（正确初始化 GeneratedClass 和事件路由） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UBlueprint* CreateActorBlueprint(const FString& AssetPath, const FString& AssetName);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<UEdGraph*> GetAllGraphs(UBlueprint* Blueprint);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddEventNode(UEdGraph* Graph, const FString& EventName,
        int32 PosX = 0, int32 PosY = 0);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddFunctionCallNode(UEdGraph* Graph,
        const FString& FunctionName, const FString& TargetClassName,
        int32 PosX = 0, int32 PosY = 0);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPNodeInfo> ListNodes(UEdGraph* Graph);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPPinInfo> ListPins(UEdGraphNode* Node);

    /** 通过 FBPNodeInfo 查询引脚信息（替代 list_pins，接受远程返回的节点信息） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPPinInfo> GetPinInfos(UEdGraph* Graph, const FBPNodeInfo& NodeInfo);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool ConnectPins(UEdGraphNode* SrcNode, const FString& SrcPin,
        UEdGraphNode* DstNode, const FString& DstPin);

    /** 断开两引脚间连线（v0.8 T-20260805 新增：真实 UE 5.1 Python 无 EdGraphPin 绑定，只能 C++ 实现） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool BreakPins(UEdGraphNode* SrcNode, const FString& SrcPin,
        UEdGraphNode* DstNode, const FString& DstPin);

    /** 从图中移除指定节点（v0.8 T-20260805 新增：真实 UE 5.1 Python 无 RemoveNode 绑定） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool RemoveNode(UEdGraph* Graph, UEdGraphNode* Node);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetPinDefaultValue(UEdGraphNode* Node,
        const FString& PinName, const FString& Value);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool CompileBlueprint(UBlueprint* Blueprint);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool CompileAndSave(UBlueprint* Blueprint);

    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddMemberVariable(UBlueprint* Blueprint,
        const FString& VarName, const FString& VarType);

    /** 给蓝图添加组件（v0.9 T-20260822 新增：C++ 侧 SCS API，绕过 Python 无 SCS 暴露限制） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddComponentToBlueprint(UBlueprint* Blueprint,
        const FString& ComponentName, const FString& ComponentClass, FString& OutError);

    /** 设置组件属性（v0.10 T-20260823 新增：C++ 侧通过 SCS 节点模板 + FindFProperty
     *  设置，取代 Python 侧 BPGC.ComponentTemplates 不可达路径；裁决 A 语义：
     *  ''=成功 / None=失败无文本 / 非空 str=失败+错误文本）
     */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetComponentProperty(UBlueprint* Blueprint,
        const FString& ComponentName, const FString& PropertyName,
        const FString& PropertyValue, FString& OutError);

    /** 读取蓝图变量清单（v0.10 T-20260823 新增：C++ 侧反射读取 UBlueprint::NewVariables
     *  为主 + GeneratedClass ChildProperties 兜底，消除 Python new_variables 属性
     *  不可达导致的假阳性/降级路径）
     */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPVariableInfo> ReadVariables(UBlueprint* Blueprint);

    // ========== 控制流节点 ==========

    /** 添加 Branch 节点 */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddBranchNode(UEdGraph* Graph, int32 PosX = 0, int32 PosY = 0);

    /** 添加 Sequence 节点 */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddSequenceNode(UEdGraph* Graph, int32 NumOutputs, int32 PosX = 0, int32 PosY = 0);

    // ========== 节点查找 ==========

    /** 清除图表中所有节点（类似手工删除后重建） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static void ClearGraphNodes(UEdGraph* Graph);

    /** 按标题查找节点（精确匹配） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* FindNodeByTitle(UEdGraph* Graph, const FString& Title);

    /** 读取图中所有连线（从 Output 侧记录，避免重复） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPConnectionInfo> GetNodeConnections(UEdGraph* Graph);

    /** 读取单个节点的所有连线（通过 FBPNodeInfo 指定节点） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPConnectionInfo> GetNodeConnectionsForNode(UEdGraph* Graph, const FBPNodeInfo& NodeInfo);

    /** 在 GameThread 上执行任意 Python 代码（线程安全入口，供 /read 等非 GameThread 端点调用） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static void ExecutePythonOnGameThread(const FString& PythonCode, FString& OutResult);

    // =====================================================================
    // T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-A · 15 个新接口
    // 设计原则（规格书 §0）：AddNodeByClass 通用打底 + 其余语义薄封装；
    // 禁止逐节点硬编码；失败一律 fail-loud（返回 nullptr/false + UE_LOG Warning）。
    // 建节点统一 5 步：AllocateDefaultPins → CreateNewGuid → Graph->AddNode →
    // 设 NodePosX/Y → Graph->NotifyGraphChanged()
    // =====================================================================

    // ---- A 批 · 通用节点层（4）----

    /** A1 通用建节点：任意 UEdGraphNode 子类一次性落图（A 批地基，解锁全部具体节点类型） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddNodeByClass(UEdGraph* Graph, TSubclassOf<UEdGraphNode> NodeClass,
        int32 PosX = 0, int32 PosY = 0);

    /** A2 设置节点坐标（NodePosX/NodePosY + NotifyGraphChanged） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetNodePosition(UEdGraphNode* Node, int32 X, int32 Y);

    /** A3 按 NodeGuid 精确查找节点（解决同名节点接错线的歧义） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* GetNodeByGuid(UEdGraph* Graph, const FString& GuidStr);

    /** A4 反射写节点属性（EnabledState / NodeComment / bCommentBubbleVisible / AdvancedPinDisplay 等） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetNodeProperty(UEdGraphNode* Node, const FString& PropertyName, const FString& Value);

    // ---- B 批 · 语义层（8）----

    /** B1 变量 Get/Set 节点（走 SetFromProperty，不手拼 FMemberReference） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddVariableNode(UEdGraph* Graph, const FString& VarName, bool bIsSetter,
        int32 PosX = 0, int32 PosY = 0);

    /** B2 动态 Cast 节点（类名支持 /Script/Engine.Actor 与 /Game/.../BP_X.BP_X_C） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddCastNode(UEdGraph* Graph, const FString& TargetClassName,
        int32 PosX = 0, int32 PosY = 0);

    /** B3 标准宏实例节点（ForEachLoop / ForEachLoopWithBreak / WhileLoop / IsValid / Gate /
     *  DoOnce / MultiGate / Sequence / FlipFlop ...） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddMacroNode(UEdGraph* Graph, const FString& MacroName,
        int32 PosX = 0, int32 PosY = 0);

    /** B4 按键事件节点（W / A / SpaceBar / LeftMouseButton / RightMouseButton ...） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddInputKeyEventNode(UEdGraph* Graph, const FString& KeyName,
        int32 PosX = 0, int32 PosY = 0);

    /** B5 InputAction 事件节点（Legacy Input Action；Enhanced Input 走本接口需实测，
     *  不可用时降级为 B4 AddInputKeyEventNode） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddInputActionEventNode(UEdGraph* Graph, const FString& ActionName,
        int32 PosX = 0, int32 PosY = 0);

    /** B6 组件委托绑定事件节点（门开关的碰撞事件依赖此接口） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddComponentBoundEventNode(UEdGraph* Graph, const FString& ComponentName,
        const FString& DelegateName, int32 PosX = 0, int32 PosY = 0);

    /** B7 Switch 节点（TypeKind ∈ {int, string, name, enum}；enum 时用 EnumOrTypeName 指定枚举） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddSwitchNode(UEdGraph* Graph, const FString& TypeKind,
        const FString& EnumOrTypeName, int32 PosX = 0, int32 PosY = 0);

    /** B8 结构体 Make/Break 节点（Vector / Rotator / Transform / HitResult ...） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddStructNode(UEdGraph* Graph, const FString& StructName, bool bIsBreak,
        int32 PosX = 0, int32 PosY = 0);

    // ---- C 批 · 蓝图 / 类层（3）----

    /** C1 写类默认值（GeneratedClass CDO → FindFProperty + ImportText；解「PC 无实例」场景） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetClassDefault(UBlueprint* Blueprint, const FString& PropertyName, const FString& Value);

    /** C2 移除 SCS 组件（语义对齐裁决 A：''=成功 / 非空=失败+错误文本） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool RemoveComponent(UBlueprint* Blueprint, const FString& ComponentName, FString& OutError);

    /** C3 蓝图实现新接口（FBlueprintEditorUtils::ImplementNewInterface） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddImplementedInterface(UBlueprint* Blueprint, const FString& InterfaceClassPath);

    // =====================================================================
    // T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-B · 17 个新接口
    //   必须（8 个 UFUNCTION）：动态引脚 3 + 注释框 1 + 自定义事件 1 +
    //                          函数参数/局部变量 2 + 事件分发器 1
    //   建议（9 个 UFUNCTION）：轴输入 / ActorBoundEvent / CreateDelegate /
    //                          Timeline / ClassCast / Composite / MathExpression /
    //                          AsyncAction / EnumLiteral
    // 说明：E1 修正（PostPlacedNewNode）已并入 AddNodeByClass 内部统一流程；
    //       分母口径（E6）为 ≈96 可实例化 K2 节点类（109 − 3 接口 − 9 抽象 − 1 废弃）
    // =====================================================================

    // ---- PATCH-B · 必须（动态引脚通用层）----

    /** B-1 给任意节点动态加引脚（Direction = input|output；PinTypeStr 语法见 cpp BridgeParsePinType）
     *  ⚠️ 签名偏离规格：规格写 UEdGraphPin* 返回，但 UEdGraphPin 非 UHT 反射类型
     *  （UHT: Unable to find 'class'/'struct' with name 'UEdGraphPin'），故改为
     *  bool 返回 + OutPinName 回填（Python 侧可拿到实际引脚名） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddPinToNode(UEdGraphNode* Node, const FString& Direction,
        const FString& PinTypeStr, const FString& PinName, FString& OutPinName);

    /** B-2 按引脚名移除节点引脚（UEdGraphNode::RemovePin） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool RemovePinFromNode(UEdGraphNode* Node, const FString& PinName);

    /** B-3 节点语义层加输入引脚（内部 Cast<IK2Node_AddPinInterface>，如 Switch / Select / FormatText） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool TryNodeAddInputPin(UEdGraphNode* Node);

    // ---- PATCH-B · 必须（语义层）----

    /** B-4 注释框（UEdGraphNode_Comment；类属 UnrealEd 模块、Python 不可达） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddCommentNode(UEdGraph* Graph, const FString& Text,
        int32 PosX = 0, int32 PosY = 0, int32 Width = 400, int32 Height = 200);

    /** B-5 自定义事件（先设 CustomFunctionName 再走 E1 顺序；可选委托签名函数全路径/短名） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddCustomEventNode(UEdGraph* Graph, const FString& EventName,
        int32 PosX = 0, int32 PosY = 0, const FString& SignatureFuncName = TEXT(""));

    /** B-6a 函数图参数引脚
     *  bIsInput = true  → FunctionEntry 入参（引脚方向 EGPD_Output，见 K2Node_FunctionEntry.cpp:430）
     *  bIsInput = false → FunctionResult 出参（引脚方向 EGPD_Input） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddFunctionPinEntry(UEdGraph* Graph, const FString& PinName,
        const FString& PinTypeStr, bool bIsInput);

    /** B-6b 函数图局部变量（写 UK2Node_FunctionEntry::LocalVariables + 重建 + 标记结构修改） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddLocalVariable(UEdGraph* Graph, const FString& VarName, const FString& VarType);

    /** B-7 事件分发器（PC_MCDelegate 成员 + DelegateSignatureGraphs）
     *  引擎无公开静态 API → 按 FBlueprintEditor::OnAddNewDelegate 路径自建
     *  语义：true = 成功；false = OutError 非空错误文本 */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddEventDispatcher(UBlueprint* Blueprint, const FString& DelegateName, FString& OutError);

    // ---- PATCH-B · 建议（9 项 · 常见能力）----

    /** S1 轴输入事件（bIsKeyAxis=true → UK2Node_InputAxisKeyEvent(FKey)；false → InputAxisEvent(FName)） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddInputAxisEventNode(UEdGraph* Graph, const FString& AxisName,
        bool bIsKeyAxis, int32 PosX = 0, int32 PosY = 0);

    /** S2 绑定关卡 Actor 实例事件（InitializeActorBoundEventParams；与 B6 组件委托语义不同） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddActorBoundEventNode(UEdGraph* Graph, const FString& ActorName,
        const FString& DelegateName, int32 PosX = 0, int32 PosY = 0);

    /** S3 CreateDelegate（Bind Event；SetFunction(FName)） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddCreateDelegateNode(UEdGraph* Graph, const FString& FunctionName,
        int32 PosX = 0, int32 PosY = 0);

    /** S4 Timeline 节点（先 AddNewTimeline 建模板，再建节点；空名自动取唯一名） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddTimelineNode(UEdGraph* Graph, const FString& TimelineName,
        int32 PosX = 0, int32 PosY = 0);

    /** S5 Class 动态 Cast（UK2Node_ClassDynamicCast，对类引用 Cast） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddClassCastNode(UEdGraph* Graph, const FString& TargetClassName,
        int32 PosX = 0, int32 PosY = 0);

    /** S6 折叠图 Composite（PostPlacedNewNode 内建 BoundGraph + Tunnel 进出节点） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddCompositeNode(UEdGraph* Graph, int32 PosX = 0, int32 PosY = 0);

    /** S7 数学表达式节点（Expression 字符串，如 "(A+B)*C"；ReconstructNode 内触发 RebuildExpression） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddMathExpressionNode(UEdGraph* Graph, const FString& Expression,
        int32 PosX = 0, int32 PosY = 0);

    /** S8 异步任务节点（ProxyFactory* 为 protected UPROPERTY，走反射写入）
     *  FactoryFunctionName: 静态工厂函数名；FactoryClassName: 宿主类全路径；
     *  ActivateFunctionName: 代理 go 函数名（可空） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddAsyncActionNode(UEdGraph* Graph, const FString& FactoryFunctionName,
        const FString& FactoryClassName, const FString& ActivateFunctionName,
        int32 PosX = 0, int32 PosY = 0);

    /** S9 枚举字面量节点（EnumName 全路径/短名；ValueName 可选，写入 Enum 引脚默认值） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UEdGraphNode* AddEnumLiteralNode(UEdGraph* Graph, const FString& EnumName,
        const FString& ValueName, int32 PosX = 0, int32 PosY = 0);
    // ============================================================
    //  UMG 控件蓝图（v3.0 · T-20260918-V3-UMG）
    //  既有节点级图操作（AddEventNode / AddFunctionCallNode / ConnectPins /
    //  SetPinDefaultValue / CompileBlueprint ...）对 WidgetBlueprint 的
    //  EventGraph 同样适用；本组补齐【资产创建 + 控件树 + 控件属性】三块
    //  （UWidgetTree 未向 Python 暴露，只能走 C++）。
    //  反射语义对齐裁决 A：'' = 成功 / 非空 str = 失败+错误文本。
    // ============================================================

    /** 创建 UMG 控件蓝图（父类 UUserWidget；自动建 WidgetTree + 编译） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static UBlueprint* CreateWidgetBlueprint(const FString& AssetPath, const FString& AssetName);

    /** 向控件树添加控件（ParentWidgetName 空 = 设为根；父须为 Panel 类控件） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool AddWidgetToTree(UBlueprint* Blueprint, const FString& ParentWidgetName,
        const FString& WidgetClassPath, const FString& WidgetName, FString& OutError);

    /** 设置控件属性（FindFProperty + ImportText_Direct，同 SetComponentProperty 模式） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool SetWidgetProperty(UBlueprint* Blueprint, const FString& WidgetName,
        const FString& PropertyName, const FString& Value, FString& OutError);

    /** 读取控件树（名称 / 类路径 / 父控件名） */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<FBPWidgetInfo> ReadWidgetTree(UBlueprint* Blueprint);
    /** v3.0（T-20260918-V3-MAT）：读取材质全部表达式。
     *  UMaterial::Expressions 为受保护成员、Python 侧无枚举接口（5.1 实测只有
     *  get_num_material_expressions）→ 由 C++ 公开 GetExpressions() 补齐。 */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static TArray<UMaterialExpression*> GetMaterialExpressions(UMaterial* Material);
    /** v3.0.1：读取控件属性（回读比对用）。返回值语义：恒返回 true，
     *  OutValue = 导出文本；失败时 OutValue = "ERROR: <原因>"（透传错误文本）。 */
    UFUNCTION(BlueprintCallable, Category = "BlueprintPythonBridge")
    static bool ReadWidgetProperty(UBlueprint* Blueprint, const FString& WidgetName,
        const FString& PropertyName, FString& OutValue);
};

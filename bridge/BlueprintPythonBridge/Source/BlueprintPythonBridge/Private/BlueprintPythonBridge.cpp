// Workaround for VS2022 14.42+ with UE5.1 engine headers
// __has_feature is a Clang built-in; MSVC doesn't recognize it in preprocessor
#ifndef __has_feature
#define __has_feature(x) 0
#endif
#pragma warning(push)
#pragma warning(disable: 4668)
#pragma warning(disable: 4067)

#include "BlueprintPythonBridge.h"
// v3.0（T-20260918-V3-UMG）：UMG 控件蓝图支持（WidgetTree 未向 Python 暴露 → C++ 补齐）
#include "WidgetBlueprint.h"
#include "Blueprint/WidgetBlueprintGeneratedClass.h"
#include "Blueprint/WidgetTree.h"
// v3.2：FSavePackageArgs（5.1/5.8 均在 UObject/SavePackage.h）
#include "UObject/SavePackage.h"
// v3.0（T-20260918-V3-MAT）：材质表达式枚举
#include "Materials/Material.h"
#include "Materials/MaterialExpression.h"
#include "Blueprint/UserWidget.h"
#include "Components/Widget.h"
#include "Components/PanelWidget.h"
#include "Components/PanelSlot.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SCS_Node.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"
#include "K2Node_Event.h"
#include "K2Node_CallFunction.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_ExecutionSequence.h"
// ---- T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-A 新增（15 接口所需节点头，集中此处）----
#include "K2Node_Variable.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_InputKey.h"
#include "K2Node_InputAction.h"
#include "K2Node_ComponentBoundEvent.h"
#include "K2Node_Switch.h"
#include "K2Node_SwitchInteger.h"
#include "K2Node_SwitchString.h"
#include "K2Node_SwitchName.h"
#include "K2Node_SwitchEnum.h"
#include "K2Node_StructOperation.h"
#include "K2Node_MakeStruct.h"
#include "K2Node_BreakStruct.h"
// ---- T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-B 新增（必须 8 + 建议 9）----
#include "K2Node_AddPinInterface.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_EditablePinBase.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_InputAxisEvent.h"
#include "K2Node_InputAxisKeyEvent.h"
#include "K2Node_ActorBoundEvent.h"
#include "K2Node_CreateDelegate.h"
#include "K2Node_Timeline.h"
#include "K2Node_ClassDynamicCast.h"
#include "K2Node_Composite.h"
#include "K2Node_MathExpression.h"
#include "K2Node_EnumLiteral.h"
#include "K2Node_AsyncAction.h"
#include "EdGraphNode_Comment.h"
#include "Engine/TimelineTemplate.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "Templates/Function.h"
#include "InputCoreTypes.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Editor.h"
#include "Components/ActorComponent.h"
#include "Components/SceneComponent.h"
#include "GameFramework/Actor.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "Misc/PackageName.h"
#include "Async/Async.h"
#include "Modules/ModuleManager.h"
#include "IPythonScriptPlugin.h"
#include "Misc/FileHelper.h"
#include "HAL/FileManager.h"
#include "UObject/UnrealType.h"
#include "UObject/EnumProperty.h"
#include "UObject/TextProperty.h"
#include "UObject/UObjectIterator.h"

// ---------- GameThread helper ----------
// If already on GameThread, execute inline; otherwise dispatch to GameThread.
template<typename Func>
auto RunOnGameThread(Func&& InFunc) -> decltype(InFunc())
{
    if (IsInGameThread())
    {
        return InFunc();
    }
    return Async(EAsyncExecution::TaskGraphMainThread, std::forward<Func>(InFunc)).Get();
}


// ========== GameThread Guard Pattern (T-20260723-CPP-FIX) ==========
// Problem: When remote_execution Python calls run on GameThread,
// Async(EAsyncExecution::TaskGraphMainThread, ...).Get() causes nested
// GameThread dispatch deadlock.
// Solution: Each write function wraps body in auto Work=[...]{...}; then:
//   if (IsInGameThread()) return Work();
//   return Async(EAsyncExecution::TaskGraphMainThread, Work).Get();

// ---------- Graph ----------

UEdGraph* UBlueprintPythonBridge::FindEventGraph(UBlueprint* Blueprint)
{
    if (!Blueprint) return nullptr;
    TArray<UEdGraph*> Graphs;
    Blueprint->GetAllGraphs(Graphs);
    for (UEdGraph* G : Graphs)
    {
        if (G && G->GetFName() == UEdGraphSchema_K2::GN_EventGraph)
            return G;
    }
    return nullptr;
}

UEdGraph* UBlueprintPythonBridge::GetEventGraph(UBlueprint* Blueprint)
{
    if (!Blueprint) return nullptr;

    auto Work = [Blueprint]() -> UEdGraph* {
        UEdGraph* Existing = FindEventGraph(Blueprint);
        if (Existing) return Existing;
        
        if (!FBlueprintEditorUtils::DoesSupportEventGraphs(Blueprint))
            return nullptr;
        
        UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
            Blueprint,
            UEdGraphSchema_K2::GN_EventGraph,
            UEdGraph::StaticClass(),
            UEdGraphSchema_K2::StaticClass()
        );
        if (NewGraph)
        {
            Blueprint->UbergraphPages.Add(NewGraph);
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        }
        return NewGraph;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

UEdGraph* UBlueprintPythonBridge::CreateEventGraph(UBlueprint* Blueprint)
{
    if (!Blueprint) return nullptr;

    auto Work = [Blueprint]() -> UEdGraph* {
        UEdGraph* Existing = FindEventGraph(Blueprint);
        if (Existing) return Existing;
        
        if (!FBlueprintEditorUtils::DoesSupportEventGraphs(Blueprint))
            return nullptr;
        
        UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
            Blueprint,
            UEdGraphSchema_K2::GN_EventGraph,
            UEdGraph::StaticClass(),
            UEdGraphSchema_K2::StaticClass()
        );
        if (NewGraph)
        {
            Blueprint->UbergraphPages.Add(NewGraph);
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        }
        return NewGraph;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

UBlueprint* UBlueprintPythonBridge::CreateActorBlueprint(const FString& AssetPath, const FString& AssetName)
{
    // T-20260723-CPP-FIX: Inline GT guard to avoid nested Async deadlock
    auto Work = [AssetPath, AssetName]() -> UBlueprint* {
        FString PackageName = AssetPath;
        if (!PackageName.StartsWith(TEXT("/Game")))
            PackageName = TEXT("/Game/") + PackageName;
        while (PackageName.EndsWith(TEXT("/")))
            PackageName = PackageName.LeftChop(1);
        PackageName = PackageName / AssetName;
        
        // T-20260724-COLLISION-FIX: 三重检查防止 FKismetEditorUtilities::CreateBlueprint
        // 内部断言 FindObject<UBlueprint>(Outer, *NewBPName) == 0 触发崩溃。
        // 根因：之前检查 FindObject(nullptr, *PackageName) 找不到 /Game/BP.BP 格式的对象。

        // ① 全局查找（全路径）
        if (FindObject<UBlueprint>(nullptr, *PackageName))
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Asset '%s' already exists in global table (full path)"), *PackageName);
            return nullptr;
        }

        // ② 已存在的 Package 内查找
        UPackage* ExistingPackage = FindPackage(nullptr, *PackageName);
        if (ExistingPackage)
        {
            UBlueprint* ExistingBP = FindObject<UBlueprint>(ExistingPackage, *AssetName);
            if (ExistingBP)
            {
                UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Blueprint '%s' already exists inside existing package '%s'"), *AssetName, *PackageName);
                return ExistingBP;
            }
        }

        UPackage* Package = CreatePackage(*PackageName);

        // ③ 新建 Package 后再查一次（对象可能在全局表中但 Package 已 GC）
        {
            UBlueprint* LeakedBP = FindObject<UBlueprint>(Package, *AssetName);
            if (LeakedBP)
            {
                UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Blueprint '%s' leaked inside new package (post-GC), reusing"), *AssetName);
                return LeakedBP;
            }
        }
        if (!Package)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] Failed to create package '%s'"), *PackageName);
            return nullptr;
        }
        
        UBlueprint* BP = FKismetEditorUtilities::CreateBlueprint(
            AActor::StaticClass(),
            Package,
            *AssetName,
            BPTYPE_Normal,
            UBlueprint::StaticClass(),
            UBlueprintGeneratedClass::StaticClass(),
            NAME_None
        );
        
        if (!BP)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] Failed to create blueprint '%s'"), *AssetName);
            return nullptr;
        }
        
        FAssetRegistryModule::AssetCreated(BP);
        FKismetEditorUtilities::CompileBlueprint(BP);
        
        if (BP->GeneratedClass)
        {
            AActor* CDO = Cast<AActor>(BP->GeneratedClass->GetDefaultObject());
            if (CDO)
            {
                CDO->PrimaryActorTick.bCanEverTick = true;
                CDO->PrimaryActorTick.bStartWithTickEnabled = true;
        
                if (!CDO->GetRootComponent())
                {
                    USceneComponent* RootComp = NewObject<USceneComponent>(
                        CDO,
                        USceneComponent::StaticClass(),
                        FName(TEXT("DefaultSceneRoot")),
                        RF_Public | RF_Transactional
                    );
                    if (RootComp)
                    {
                        RootComp->SetMobility(EComponentMobility::Movable);
                        RootComp->CreationMethod = EComponentCreationMethod::SimpleConstructionScript;
                        CDO->SetRootComponent(RootComp);
                        UE_LOG(LogTemp, Log, TEXT("[BPBridge] Added DefaultSceneRoot to '%s'"), *AssetName);
                    }
                }
            }
        }
        
        BP->MarkPackageDirty();
        FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
        
        FString File;
        // v3.2 5.8 兼容：DoesPackageExist 的 Guid 重载已移除；SavePackage 改为
        // FSavePackageArgs 现代重载（5.1 亦支持该重载 → 单套代码跨版本）
        if (FPackageName::DoesPackageExist(Package->GetName(), &File))
        {
            FSavePackageArgs SaveArgs;
            SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
            SaveArgs.Error = GError;
            SaveArgs.SaveFlags = SAVE_NoError;
            UPackage::SavePackage(Package, BP, *File, SaveArgs);
        }
        
        return BP;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

TArray<UEdGraph*> UBlueprintPythonBridge::GetAllGraphs(UBlueprint* Blueprint)
{
    TArray<UEdGraph*> R;
    if (Blueprint) Blueprint->GetAllGraphs(R);
    return R;
}

// ---------- Events ----------

UEdGraphNode* UBlueprintPythonBridge::AddEventNode(
    UEdGraph* Graph, const FString& EventName, int32 PosX, int32 PosY)
{
    if (!Graph) return nullptr;

    auto Work = [Graph, EventName, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP) return nullptr;
        
        UClass* SearchClass = BP->GeneratedClass ? BP->GeneratedClass : BP->ParentClass;
        UFunction* Func = nullptr;
        
        for (UClass* C = SearchClass; C && !Func; C = C->GetSuperClass())
        {
            for (TFieldIterator<UFunction> It(C, EFieldIteratorFlags::IncludeSuper); It; ++It)
            {
                if (It->HasAnyFunctionFlags(FUNC_BlueprintEvent) &&
                    It->GetName().Equals(EventName, ESearchCase::IgnoreCase))
                {
                    Func = *It;
                    break;
                }
            }
        }
        
        if (!Func)
        {
            for (TFieldIterator<UFunction> It(AActor::StaticClass(), EFieldIteratorFlags::IncludeSuper); It; ++It)
            {
                if (It->HasAnyFunctionFlags(FUNC_BlueprintEvent) &&
                    It->GetName().Equals(EventName, ESearchCase::IgnoreCase))
                {
                    Func = *It;
                    break;
                }
            }
        }
        
        if (!Func)
        {
            for (TFieldIterator<UFunction> It(UActorComponent::StaticClass(), EFieldIteratorFlags::IncludeSuper); It; ++It)
            {
                if (It->HasAnyFunctionFlags(FUNC_BlueprintEvent) &&
                    It->GetName().Equals(EventName, ESearchCase::IgnoreCase))
                {
                    Func = *It;
                    break;
                }
            }
        }
        
        if (!Func)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Event '%s' not found"), *EventName);
            return nullptr;
        }
        
        UK2Node_Event* Node = NewObject<UK2Node_Event>(Graph);
        Node->EventReference.SetExternalMember(Func->GetFName(), Func->GetOuterUClass());
        Node->bOverrideFunction = true;
        Node->NodePosX = PosX;
        Node->NodePosY = PosY;
        Graph->AddNode(Node, false, false);
        Node->CreateNewGuid();
        Node->PostPlacedNewNode();
        Node->AllocateDefaultPins();
        Graph->Modify();
        Node->Modify();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Function Call ----------

UEdGraphNode* UBlueprintPythonBridge::AddFunctionCallNode(
    UEdGraph* Graph, const FString& FunctionName,
    const FString& TargetClassName, int32 PosX, int32 PosY)
{
    if (!Graph) return nullptr;

    auto Work = [Graph, FunctionName, TargetClassName, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP) return nullptr;
        
        UFunction* Func = nullptr;
        
        if (!TargetClassName.IsEmpty())
        {
            UClass* TargetClass = FindObject<UClass>(nullptr, *TargetClassName);
            if (!TargetClass)
                TargetClass = LoadObject<UClass>(nullptr, *TargetClassName);
            if (TargetClass)
            {
                Func = TargetClass->FindFunctionByName(*FunctionName);
                if (Func && !Func->HasAnyFunctionFlags(FUNC_BlueprintCallable | FUNC_BlueprintPure))
                    Func = nullptr;
            }
        }
        
        if (!Func)
        {
            static const TArray<FName> SafeClasses = {
                TEXT("KismetSystemLibrary"),
                TEXT("KismetMathLibrary"),
                TEXT("GameplayStatics"),
                TEXT("KismetArrayLibrary"),
                TEXT("KismetStringLibrary"),
                TEXT("KismetTextLibrary"),
                TEXT("AIBlueprintHelperLibrary"),
                TEXT("WidgetBlueprintLibrary"),
                TEXT("KismetInputLibrary"),
                TEXT("KismetGuidLibrary"),
                TEXT("KismetNodeHelperLibrary"),
                TEXT("NavigationSystemV1"),
                TEXT("HeadMountedDisplayFunctionLibrary")
            };
            for (const FName& ClassName : SafeClasses)
            {
                UClass* SafeClass = FindObject<UClass>(nullptr, *ClassName.ToString());
                if (!SafeClass)
                    SafeClass = LoadObject<UClass>(nullptr, *ClassName.ToString());
                if (SafeClass)
                {
                    UFunction* F = SafeClass->FindFunctionByName(*FunctionName);
                    if (F && F->HasAnyFunctionFlags(FUNC_BlueprintCallable | FUNC_BlueprintPure))
                    {
                        Func = F;
                        break;
                    }
                }
            }
        }
        
        if (!Func && BP->ParentClass)
        {
            Func = BP->ParentClass->FindFunctionByName(*FunctionName);
            if (Func && !Func->HasAnyFunctionFlags(FUNC_BlueprintCallable | FUNC_BlueprintPure))
                Func = nullptr;
        }
        
        if (!Func)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Function '%s' not found"), *FunctionName);
            return nullptr;
        }
        
        UK2Node_CallFunction* Node = NewObject<UK2Node_CallFunction>(Graph);
        Node->SetFromFunction(Func);
        Node->NodePosX = PosX;
        Node->NodePosY = PosY;
        Graph->AddNode(Node, false, false);
        Node->CreateNewGuid();
        Node->PostPlacedNewNode();
        Node->AllocateDefaultPins();
        Graph->Modify();
        Node->Modify();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Control Flow ----------

UEdGraphNode* UBlueprintPythonBridge::AddBranchNode(UEdGraph* Graph, int32 PosX, int32 PosY)
{
    if (!Graph) return nullptr;

    auto Work = [Graph, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP) return nullptr;
        
        UK2Node_IfThenElse* Node = NewObject<UK2Node_IfThenElse>(Graph);
        Node->NodePosX = PosX;
        Node->NodePosY = PosY;
        Graph->AddNode(Node, false, false);
        Node->CreateNewGuid();
        Node->PostPlacedNewNode();
        Node->AllocateDefaultPins();
        Graph->Modify();
        Node->Modify();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

UEdGraphNode* UBlueprintPythonBridge::AddSequenceNode(UEdGraph* Graph, int32 NumOutputs, int32 PosX, int32 PosY)
{
    if (!Graph || NumOutputs < 1) return nullptr;

    auto Work = [Graph, NumOutputs, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP) return nullptr;
        
        UK2Node_ExecutionSequence* Node = NewObject<UK2Node_ExecutionSequence>(Graph);
        Node->NodePosX = PosX;
        Node->NodePosY = PosY;
        Graph->AddNode(Node, false, false);
        Node->CreateNewGuid();
        Node->PostPlacedNewNode();
        Node->AllocateDefaultPins();
        
        int32 ExistingOutputs = 0;
        for (UEdGraphPin* Pin : Node->Pins)
            if (Pin->Direction == EGPD_Output)
                ++ExistingOutputs;
        
        for (int32 i = ExistingOutputs; i < NumOutputs; ++i)
            Node->AddInputPin();
        
        Graph->Modify();
        Node->Modify();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Node Lookup ----------

void UBlueprintPythonBridge::ClearGraphNodes(UEdGraph* Graph)
{
    if (!Graph) return;

    auto Work = [Graph]() {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        
        Graph->Modify();
        TArray<UEdGraphNode*> NodesToRemove = Graph->Nodes;
        for (UEdGraphNode* Node : NodesToRemove)
        {
            if (!IsValid(Node)) continue;
            Node->Modify();
            if (BP)
                FBlueprintEditorUtils::RemoveNode(BP, Node);
            else
            {
                Graph->RemoveNode(Node);
                Node->DestroyNode();
            }
        }
        Graph->NotifyGraphChanged();
        
        if (BP)
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
    };
    if (IsInGameThread()) { Work(); return; }
    Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

UEdGraphNode* UBlueprintPythonBridge::FindNodeByTitle(UEdGraph* Graph, const FString& Title)
{
    if (!Graph) return nullptr;
    for (UEdGraphNode* Node : Graph->Nodes)
    {
        if (IsValid(Node) && Node->GetNodeTitle(ENodeTitleType::FullTitle).ToString().Equals(Title, ESearchCase::IgnoreCase))
            return Node;
    }
    return nullptr;
}

TArray<FBPNodeInfo> UBlueprintPythonBridge::ListNodes(UEdGraph* Graph)
{
    TArray<FBPNodeInfo> R;
    if (!Graph) return R;
    for (UEdGraphNode* N : Graph->Nodes)
    {
        if (!IsValid(N)) continue;
        FBPNodeInfo Info;
        Info.NodeTitle = N->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
        Info.NodeClass  = N->GetClass()->GetName();
        Info.PosX = N->NodePosX;
        Info.PosY = N->NodePosY;
        Info.NodeGuid = N->NodeGuid;
        Info.GraphName = Graph->GetName();
        R.Add(Info);
    }
    return R;
}

// ---------- Pins ----------

TArray<FBPPinInfo> UBlueprintPythonBridge::ListPins(UEdGraphNode* Node)
{
    TArray<FBPPinInfo> R;
    if (!Node) return R;
    for (UEdGraphPin* P : Node->Pins)
    {
        if (!P) continue;
        FBPPinInfo Info;
        Info.PinName        = P->PinName.ToString();
        Info.PinDirection   = (P->Direction == EGPD_Input) ? TEXT("Input") : TEXT("Output");
        Info.PinType        = P->PinType.PinCategory.ToString();
        Info.bIsExecutionPin = (P->PinType.PinCategory == UEdGraphSchema_K2::PC_Exec);
        Info.bIsConnected   = P->LinkedTo.Num() > 0;
        Info.PinId          = P->PinId.ToString();
        Info.DefaultValue   = P->DefaultValue;
        Info.PinSubCategory = P->PinType.PinSubCategoryObject.IsValid()
            ? P->PinType.PinSubCategoryObject->GetName()
            : TEXT("");
        R.Add(Info);
    }
    return R;
}

TArray<FBPPinInfo> UBlueprintPythonBridge::GetPinInfos(UEdGraph* Graph, const FBPNodeInfo& NodeInfo)
{
    TArray<FBPPinInfo> Result;
    if (!Graph) return Result;

    // Step 1: Find real EdGraphNode by node_guid
    UEdGraphNode* RealNode = nullptr;
    if (NodeInfo.NodeGuid.IsValid())
    {
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (IsValid(N) && N->NodeGuid == NodeInfo.NodeGuid)
            {
                RealNode = N;
                break;
            }
        }
    }

    // Fallback: match by title
    if (!RealNode)
    {
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (IsValid(N) && N->GetNodeTitle(ENodeTitleType::FullTitle).ToString().Equals(NodeInfo.NodeTitle, ESearchCase::IgnoreCase))
            {
                RealNode = N;
                break;
            }
        }
    }

    if (!RealNode) return Result;

    return ListPins(RealNode);
}

bool UBlueprintPythonBridge::ConnectPins(
    UEdGraphNode* SrcNode, const FString& SrcPin,
    UEdGraphNode* DstNode, const FString& DstPin)
{
    if (!SrcNode || !DstNode) return false;

    auto Work = [=]() -> bool {
        // FIX-B: 同图校验 — 跨图 pin 连接无意义，提前拦截
        if (SrcNode->GetGraph() != DstNode->GetGraph())
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] ConnectPins FAILED: Nodes in different graphs"));
            return false;
        }
        
        UEdGraphPin* A = SrcNode->FindPin(SrcPin, EGPD_Output);
        UEdGraphPin* B = DstNode->FindPin(DstPin, EGPD_Input);
        
        if (!A || !B)
        {
            A = SrcNode->FindPin(SrcPin);
            B = DstNode->FindPin(DstPin);
            if (!A || !B)
            {
                UE_LOG(LogTemp, Error, TEXT("[BPBridge] ConnectPins FAILED: Pin not found '%s.%s' or '%s.%s'"),
                    *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
                return false;
            }
            if (A->Direction != EGPD_Output) Swap(A, B);
        }
        
        const UEdGraphSchema* Schema = SrcNode->GetGraph()->GetSchema();
        if (!Schema)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] ConnectPins FAILED: No Schema for graph '%s'"),
                *SrcNode->GetGraph()->GetName());
            return false;
        }
        
        FPinConnectionResponse Rsp = Schema->CanCreateConnection(A, B);
        // FIX-C: 允许 MAKE_WITH_CONVERSION_NODE（隐式转换节点），配合 FIX-A 的 Modify() 可安全创建
        if (Rsp.Response != CONNECT_RESPONSE_MAKE && Rsp.Response != CONNECT_RESPONSE_MAKE_WITH_CONVERSION_NODE)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] ConnectPins FAILED: CanCreateConnection rejected '%s.%s' -> '%s.%s'. Reason: %s"),
                *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin,
                *Rsp.Message.ToString());
            return false;
        }
        
        // T-20260724-CONNECT-FIX: 删除 TryCreateConnection，只用 MakeLinkTo。
        // TryCreateConnection 内部会做额外的 schema 级连接逻辑（如 autocasting），
        // 与外部 MakeLinkTo 冲突导致双重连接或连接失败。MakeLinkTo 直接操作 pin
        // 链表，经 UE 社区验证是 ConnectPins 的正确做法。
        
        // FIX-A: Graph->Modify() 防 GC 悬空指针 — 跨类型 pin 连接时 MakeLinkTo
        // 内部可能创建隐式转换节点，必须调 Modify() 标记图已修改，否则新节点
        // 不受 GC 保护 → 悬空指针崩溃。
        if (UEdGraph* Graph = SrcNode->GetGraph())
        {
            Graph->Modify();
            SrcNode->Modify();
            DstNode->Modify();
        }
        
        A->MakeLinkTo(B);
        B->MakeLinkTo(A);
        
        if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(SrcNode->GetGraph()))
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        
        if (!A->LinkedTo.Contains(B))
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] ConnectPins FAILED after MakeLinkTo: '%s.%s' -> '%s.%s'"),
                *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
            return false;
        }
        
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] ConnectPins OK: '%s.%s' -> '%s.%s'"),
            *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ========== BreakPins (v0.8 T-20260805 新增) ==========
bool UBlueprintPythonBridge::BreakPins(
    UEdGraphNode* SrcNode, const FString& SrcPin,
    UEdGraphNode* DstNode, const FString& DstPin)
{
    if (!SrcNode || !DstNode) return false;

    auto Work = [=]() -> bool {
        // 同图校验（KB：跨图 Pin 操作必须校验同图归属）
        if (SrcNode->GetGraph() != DstNode->GetGraph())
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] BreakPins FAILED: Nodes in different graphs"));
            return false;
        }

        UEdGraphPin* A = SrcNode->FindPin(SrcPin);
        UEdGraphPin* B = DstNode->FindPin(DstPin);
        if (!A || !B)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] BreakPins FAILED: Pin not found '%s.%s' or '%s.%s'"),
                *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
            return false;
        }

        // FIX-A 模式：修改前 Modify() 保护事务/GC
        if (UEdGraph* Graph = SrcNode->GetGraph())
        {
            Graph->Modify();
            SrcNode->Modify();
            DstNode->Modify();
        }

        bool Broken = false;
        if (A->LinkedTo.Contains(B)) { A->BreakLinkTo(B); Broken = true; }
        if (B->LinkedTo.Contains(A)) { B->BreakLinkTo(A); Broken = true; }

        if (Broken)
        {
            if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(SrcNode->GetGraph()))
                FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
            UE_LOG(LogTemp, Log, TEXT("[BPBridge] BreakPins OK: '%s.%s' -> '%s.%s'"),
                *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
        }
        else
        {
            UE_LOG(LogTemp, Log, TEXT("[BPBridge] BreakPins no-op (already disconnected): '%s.%s' -> '%s.%s'"),
                *SrcNode->GetName(), *SrcPin, *DstNode->GetName(), *DstPin);
        }
        return Broken;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ========== RemoveNode (v0.8 T-20260805 新增) ==========
bool UBlueprintPythonBridge::RemoveNode(UEdGraph* Graph, UEdGraphNode* Node)
{
    if (!Graph || !Node) return false;

    auto Work = [=]() -> bool {
        if (Node->GetGraph() != Graph)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] RemoveNode FAILED: node not in given graph"));
            return false;
        }

        Graph->Modify();
        Node->Modify();

        // 断开所有引脚连线
        for (UEdGraphPin* Pin : Node->Pins)
        {
            if (Pin) Pin->BreakAllPinLinks(false);
        }

        // UE5.1 签名: RemoveNode(UBlueprint*, UEdGraphNode*, bool bDontRecompile)
        if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph))
        {
            FBlueprintEditorUtils::RemoveNode(BP, Node, true);
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        }

        UE_LOG(LogTemp, Log, TEXT("[BPBridge] RemoveNode OK: %s"), *Node->GetName());
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

bool UBlueprintPythonBridge::SetPinDefaultValue(
    UEdGraphNode* Node, const FString& PinName, const FString& Value)
{
    if (!Node) return false;

    auto Work = [Node, PinName, Value]() -> bool {
        UEdGraphPin* Pin = Node->FindPin(PinName, EGPD_Input);
        if (!Pin) return false;
        
        const UEdGraphSchema* Schema = Node->GetGraph()->GetSchema();
        if (Schema)
        {
            Schema->TrySetDefaultValue(*Pin, Value);
            if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Node->GetGraph()))
                FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
            return true;
        }
        return false;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Compile ----------

bool UBlueprintPythonBridge::CompileBlueprint(UBlueprint* Blueprint)
{
    if (!Blueprint) return false;

    auto Work = [Blueprint]() -> bool {
        FKismetEditorUtilities::CompileBlueprint(Blueprint);
        bool bOk = (Blueprint->Status == BS_UpToDate || Blueprint->Status == BS_Dirty);
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] Compile %s for '%s' (Status=%d)"),
            bOk ? TEXT("SUCCESS") : TEXT("FAILED"),
            *Blueprint->GetName(), (int32)Blueprint->Status);
        return bOk;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

bool UBlueprintPythonBridge::CompileAndSave(UBlueprint* Blueprint)
{
    if (!Blueprint) return false;

    auto Work = [Blueprint]() -> bool {
        FKismetEditorUtilities::CompileBlueprint(Blueprint);
        if (Blueprint->Status == BS_Unknown
         || Blueprint->Status == BS_Error
         || Blueprint->Status == BS_BeingCreated) return false;
        
        UPackage* Pkg = Blueprint->GetOutermost();
        if (!Pkg) return false;
        FString File;
        // v3.2 5.8 兼容：同上（Guid 重载移除 + FSavePackageArgs 现代重载）
        if (!FPackageName::DoesPackageExist(Pkg->GetName(), &File)) return false;
        FSavePackageArgs SaveArgs;
        SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
        SaveArgs.Error = GError;
        SaveArgs.SaveFlags = SAVE_NoError;
        return UPackage::SavePackage(Pkg, Blueprint, *File, SaveArgs);
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

bool UBlueprintPythonBridge::AddMemberVariable(
    UBlueprint* Blueprint, const FString& VarName, const FString& TypeStr)
{
    if (!Blueprint) return false;

    auto Work = [Blueprint, VarName, TypeStr]() -> bool {
        FEdGraphPinType PinType;
        FString T = TypeStr.ToLower();
        
        if      (T == TEXT("bool")   || T == TEXT("boolean"))    PinType.PinCategory = UEdGraphSchema_K2::PC_Boolean;
        else if (T == TEXT("int")    || T == TEXT("int32") || T == TEXT("integer")) PinType.PinCategory = UEdGraphSchema_K2::PC_Int;
        else if (T == TEXT("float"))                             {PinType.PinCategory = UEdGraphSchema_K2::PC_Real; PinType.PinSubCategory = UEdGraphSchema_K2::PC_Float;}
        else if (T == TEXT("double"))                            {PinType.PinCategory = UEdGraphSchema_K2::PC_Real; PinType.PinSubCategory = UEdGraphSchema_K2::PC_Double;}
        else if (T == TEXT("string") || T == TEXT("fstring") || T == TEXT("ftext") || T == TEXT("fname"))
                                                                  PinType.PinCategory = UEdGraphSchema_K2::PC_String;
        else if (T == TEXT("vector")  || T == TEXT("fvector"))   {PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FVector>::Get();}
        else if (T == TEXT("rotator") || T == TEXT("frotator"))  {PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FRotator>::Get();}
        else if (T == TEXT("transform")||T == TEXT("ftransform")){PinType.PinCategory = UEdGraphSchema_K2::PC_Struct; PinType.PinSubCategoryObject = TBaseStructure<FTransform>::Get();}
        else
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] Unknown type '%s' for variable '%s'"), *TypeStr, *VarName);
            return false;
        }
        
        bool Ok = FBlueprintEditorUtils::AddMemberVariable(Blueprint, *VarName, PinType);
        if (Ok) FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        return Ok;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Components (T-20260822-ADDCOMPONENT-CPP) ----------
// 背景：UE5.1 Python 不暴露 SimpleConstructionScript，Python 侧 add_component
// 一直失败（KB UE5_Python_API_组件添加限制_SCS未暴露.md [已验证]）。本方法在
// C++ 侧完成 SCS 节点注册，Python 只需调用 bridge.add_component_to_blueprint。
// API 签名现场核实（<UE_ROOT>\Engine\Source\）：
//   - USimpleConstructionScript::CreateNode(UClass*, FName) —— SimpleConstructionScript.cpp L1372
//     内部已自动：NewObject 模板(RF_ArchetypeObject|RF_Transactional|RF_Public) →
//     Rename 到 Blueprint->GeneratedClass → CreateNodeImpl 绑定 ComponentClass/Template。
//     因此无需手动 NewObject + AddComponentTemplate（5.1 已无该 API，BPGC 用 ComponentTemplates 数组）。
//   - USimpleConstructionScript::AddNode(USCS_Node*) —— SimpleConstructionScript.h L103
bool UBlueprintPythonBridge::AddComponentToBlueprint(
    UBlueprint* Blueprint, const FString& ComponentName,
    const FString& ComponentClass, FString& OutError)
{
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }

    auto Work = [Blueprint, ComponentName, ComponentClass, &OutError]() -> bool {
        if (ComponentName.IsEmpty())
        {
            OutError = TEXT("ComponentName is empty");
            return false;
        }

        // ① 解析组件类：支持 "StaticMeshComponent" 短名与 "/Script/Engine.StaticMeshComponent" 全路径
        UClass* Cls = nullptr;
        FString ClassPath = ComponentClass;
        if (ClassPath.StartsWith(TEXT("/Script/")) || ClassPath.StartsWith(TEXT("/Engine/")))
        {
            Cls = FindObject<UClass>(nullptr, *ClassPath);
            if (!Cls)
            {
                Cls = LoadClass<UObject>(nullptr, *ClassPath);
            }
        }
        else
        {
            // 短名：引擎启动时所有类已注册，FindFirstObject 全局查找（5.1 替代已弃用的 ANY_PACKAGE）
            Cls = FindFirstObject<UClass>(*ClassPath);
        }
        if (!Cls || !Cls->IsChildOf(UActorComponent::StaticClass()))
        {
            OutError = FString::Printf(
                TEXT("Component class not found or not an ActorComponent: %s"), *ComponentClass);
            return false;
        }

        // ② 取 SCS（C++ 可访问；null 说明蓝图尚未初始化 SCS）
        USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
        if (!SCS)
        {
            OutError = TEXT("Blueprint has no SimpleConstructionScript");
            return false;
        }

        // ②.1 D-1 修复（T-20260910-UE5BRIDGE-D1D2-FIX）：SCS 外链自愈 + fail-loud
        //   事故（P0 崩溃级）：SCS->GetBlueprint() 返回 null 时，引擎
        //   USimpleConstructionScript::CreateNode() 内部 check(Blueprint) 直接崩编辑器
        //   （SimpleConstructionScript.cpp:1375），未存盘进度全丢。
        //   引擎事实：GetBlueprint() = Cast<UBlueprint>(GetOwnerClass()->ClassGeneratedBy)，
        //   任一环断链即为 null。此处先尝试自愈（改用 GeneratedClass / SkeletonGeneratedClass
        //   上挂载的正规 SCS），仍失败则 fail-loud —— 绝不把断言崩溃留给调用方。
        //   fail-loud 语义对齐裁决 A：return true + 非空 OutError（Python 侧才能读到文本）。
        {
            UBlueprint* SCSOwner = SCS->GetBlueprint();
            if (!SCSOwner)
            {
                auto TryAdoptSCS = [&SCS, &SCSOwner, Blueprint](UClass* CandidateClass) -> bool
                {
                    UBlueprintGeneratedClass* CandidateBPGC = Cast<UBlueprintGeneratedClass>(CandidateClass);
                    if (!CandidateBPGC || !CandidateBPGC->SimpleConstructionScript)
                    {
                        return false;
                    }
                    USimpleConstructionScript* Candidate = CandidateBPGC->SimpleConstructionScript;
                    if (Candidate == SCS)
                    {
                        return false;
                    }
                    // 候选 SCS 自身外链也必须完好，否则不采纳（避免换了个坏 SCS）
                    UBlueprint* CandidateOwner = Candidate->GetBlueprint();
                    if (!CandidateOwner)
                    {
                        return false;
                    }
                    UE_LOG(LogTemp, Warning,
                        TEXT("[BPBridge] AddComponentToBlueprint: SCS outer chain broken, self-healed to SCS on '%s'"),
                        *CandidateBPGC->GetName());
                    SCS = Candidate;
                    Blueprint->SimpleConstructionScript = Candidate;
                    SCSOwner = CandidateOwner;
                    return true;
                };

                // 自愈①：GeneratedClass 上的 SCS（引擎 Kismet2.cpp:452 的正规挂载点）
                // 自愈②：SkeletonGeneratedClass 上的 SCS
                if (!TryAdoptSCS(Blueprint->GeneratedClass))
                {
                    TryAdoptSCS(Blueprint->SkeletonGeneratedClass);
                }
            }

            if (!SCSOwner)
            {
                OutError = FString::Printf(
                    TEXT("SimpleConstructionScript has no owning Blueprint (GetBlueprint()==null) - "
                         "refused to create SCS node (engine check() at SimpleConstructionScript.cpp:1375 "
                         "would hard-crash the editor). diag: SCS='%s' outer='%s' GeneratedClass='%s' "
                         "SkeletonGeneratedClass='%s'"),
                    *SCS->GetName(),
                    SCS->GetOuter() ? *SCS->GetOuter()->GetName() : TEXT("<null>"),
                    Blueprint->GeneratedClass ? *Blueprint->GeneratedClass->GetName() : TEXT("<null>"),
                    Blueprint->SkeletonGeneratedClass ? *Blueprint->SkeletonGeneratedClass->GetName() : TEXT("<null>"));
                return true;
            }
        }

        // ②.5 重名校验（T-20260822-ADDCOMPONENT-ONLINE 裁决 B）：SCS 已有
        //     同名节点（VariableName 相等）→ 严格拒绝（幂等 skip 语义未采用）。
        //     ⚠️ UE5.1 Python 绑定行为实锤（引擎源码 PyGenUtil.cpp
        //     PackReturnValues）：bool 主返回 + FString& out 参数时，
        //     bool=false → Python 返回 None（OutError 被吞，无论是否赋值）；
        //     bool=true → Python 返回 OutError 值。因此这里必须 return true
        //     + OutError 非空，Python 侧才能拿到非空错误文本（对齐裁决 A：
        //     非空 str = 失败 + 错误文本）。T-20260822-ADDCOMPONENT-RENAME-ERR
        {
            const TArray<USCS_Node*>& ExistingNodes = SCS->GetAllNodes();
            for (const USCS_Node* Node : ExistingNodes)
            {
                if (Node && Node->GetVariableName() == FName(*ComponentName))
                {
                    OutError = FString::Printf(
                        TEXT("Component '%s' already exists in SimpleConstructionScript"),
                        *ComponentName);
                    return true;
                }
            }
        }

        // ③ 创建 SCS 节点（内部完成模板对象创建 + 绑定 GeneratedClass）
        USCS_Node* Node = SCS->CreateNode(Cls, FName(*ComponentName));
        if (!Node)
        {
            OutError = FString::Printf(TEXT("Failed to create SCS node for '%s'"), *ComponentName);
            return false;
        }

        // ④ 注册节点到 SCS
        SCS->AddNode(Node);

        // ⑤ 编译 + 脏标记
        FKismetEditorUtilities::CompileBlueprint(Blueprint);
        Blueprint->MarkPackageDirty();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);

        OutError = TEXT("");
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Component Properties (T-20260823-UE5BRIDGE-CPP-APIS 子项1) ----------
// 背景：裁决 C（T-20260822-ADDCOMPONENT-ONLINE）确认 Python 侧 BPGC.ComponentTemplates
// 不可达 → properties 设置静默失效 → 过渡 fail-loud。本方法在 C++ 侧通过 SCS 节点
// 模板（USCS_Node::GetComponentTemplate）直接设置 UPROPERTY，Python 调用
// bridge.set_component_property 逐属性设置。
// 语义（对齐裁决 A）：''=成功 / None=失败无文本（bool=false 类）/ 非空 str=失败+错误文本
//   ⚠️ 与 AddComponentToBlueprint 同款 UE Python 反射规则（PyGenUtil.cpp PackReturnValues）：
//   bool=false → Python None（OutError 被吞）；bool=true → Python 返回 OutError 值。
//   因此一切「需透传错误文本」的失败必须 return true + OutError 非空。
namespace
{
    // 数值向量解析：支持 "100 100 100" / "100,100,100" / "(X=100,Y=100,Z=100)"
    //  / "X=100 Y=100 Z=100"。返回是否解析出 >= Expected 个合法数值。
    bool ParseNumericVector(const FString& InStr, int32 Expected, TArray<double>& OutValues)
    {
        FString S = InStr.TrimStartAndEnd();
        if (S.Len() >= 2 && S.StartsWith(TEXT("(")) && S.EndsWith(TEXT(")")))
        {
            S = S.Mid(1, S.Len() - 2);
        }
        TArray<FString> Tokens;
        if (S.Contains(TEXT(",")))
        {
            S.ParseIntoArray(Tokens, TEXT(","), true);
        }
        else
        {
            S.ParseIntoArray(Tokens, TEXT(" "), true);
        }
        OutValues.Reset();
        for (const FString& RawToken : Tokens)
        {
            FString Token = RawToken.TrimStartAndEnd();
            if (Token.IsEmpty()) continue;
            int32 EqIdx = INDEX_NONE;
            if (Token.FindChar(TEXT('='), EqIdx))
            {
                Token = Token.Mid(EqIdx + 1).TrimStartAndEnd();
            }
            if (Token.IsEmpty()) continue;
            // 合法数值 token 检查（数字/小数点/正负号/科学计数）
            bool bHasDigit = false;
            bool bValid = true;
            for (int32 i = 0; i < Token.Len(); ++i)
            {
                TCHAR C = Token[i];
                if (FChar::IsDigit(C)) { bHasDigit = true; continue; }
                if (C == TEXT('.') || C == TEXT('-') || C == TEXT('+')
                    || C == TEXT('e') || C == TEXT('E')) { continue; }
                bValid = false;
                break;
            }
            if (!bValid || !bHasDigit) return false;
            OutValues.Add(FCString::Atod(*Token));
        }
        return OutValues.Num() >= Expected;
    }
}

bool UBlueprintPythonBridge::SetComponentProperty(
    UBlueprint* Blueprint, const FString& ComponentName,
    const FString& PropertyName, const FString& PropertyValue,
    FString& OutError)
{
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }

    auto Work = [Blueprint, ComponentName, PropertyName, PropertyValue, &OutError]() -> bool {
        if (ComponentName.IsEmpty() || PropertyName.IsEmpty())
        {
            OutError = TEXT("ComponentName and PropertyName must be non-empty");
            return true;
        }

        // ① 取 SCS
        USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
        if (!SCS)
        {
            OutError = TEXT("Blueprint has no SimpleConstructionScript");
            return true;
        }

        // ② 按名定位组件节点（重名冲突已由 AddComponentToBlueprint 严格拒绝）
        USCS_Node* TargetNode = nullptr;
        const TArray<USCS_Node*>& Nodes = SCS->GetAllNodes();
        for (USCS_Node* Node : Nodes)
        {
            if (Node && Node->GetVariableName() == FName(*ComponentName))
            {
                TargetNode = Node;
                break;
            }
        }
        if (!TargetNode)
        {
            OutError = FString::Printf(
                TEXT("Component '%s' not found in SimpleConstructionScript"), *ComponentName);
            return true;
        }

        // ③ 取组件模板（属性修改作用于模板 archetype → 编译后实例化组件从模板复制）
        UActorComponent* Template = TargetNode->ComponentTemplate;
        if (!Template)
        {
            OutError = FString::Printf(
                TEXT("Component '%s' has no template object (blueprint not compiled?)"), *ComponentName);
            return true;
        }

        // ④ 按名精确匹配 UPROPERTY（FindFProperty 反射查找，不受访问控制限制）
        FProperty* Prop = FindFProperty<FProperty>(Template->GetClass(), FName(*PropertyName));
        if (!Prop)
        {
            OutError = FString::Printf(
                TEXT("Property '%s' not found on component '%s' (class %s)"),
                *PropertyName, *ComponentName, *Template->GetClass()->GetName());
            return true;
        }
        // 仅允许设置可编辑属性（防写入受保护/内部属性）
        if (!Prop->HasAnyPropertyFlags(CPF_Edit))
        {
            OutError = FString::Printf(
                TEXT("Property '%s' on component '%s' is not editable"),
                *PropertyName, *ComponentName);
            return true;
        }

        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Template);

        // ⑤ 值解析：FVector/FRotator 显式数值解析（容错多种格式），其余走 ImportText
        if (FStructProperty* SP = CastField<FStructProperty>(Prop))
        {
            TArray<double> Values;
            if (SP->Struct == TBaseStructure<FVector>::Get())
            {
                if (!ParseNumericVector(PropertyValue, 3, Values))
                {
                    OutError = FString::Printf(
                        TEXT("Invalid FVector value '%s' for property '%s' (expect 3 numbers, e.g. \"100 100 100\")"),
                        *PropertyValue, *PropertyName);
                    return true;
                }
                FVector V((float)Values[0], (float)Values[1], (float)Values[2]);
                *static_cast<FVector*>(ValuePtr) = V;
            }
            else if (SP->Struct == TBaseStructure<FRotator>::Get())
            {
                if (!ParseNumericVector(PropertyValue, 3, Values))
                {
                    OutError = FString::Printf(
                        TEXT("Invalid FRotator value '%s' for property '%s' (expect 3 numbers, e.g. \"0 0 0\")"),
                        *PropertyValue, *PropertyName);
                    return true;
                }
                FRotator R((float)Values[0], (float)Values[1], (float)Values[2]);
                *static_cast<FRotator*>(ValuePtr) = R;
            }
            else
            {
                // 其他 struct（FTransform/FLinearColor 等）走 ImportText_Direct
                // （ImportText 已弃用，UE5.1 建议 ImportText_Direct/InContainer）
                const TCHAR* Result = Prop->ImportText_Direct(*PropertyValue, ValuePtr, Template, 0);
                if (Result == nullptr || *Result != 0)
                {
                    OutError = FString::Printf(
                        TEXT("Failed to parse value '%s' for property '%s'"),
                        *PropertyValue, *PropertyName);
                    return true;
                }
            }
        }
        else
        {
            // 标量属性（bool/int/float/double/string/name/enum/object 等）
            const TCHAR* Result = Prop->ImportText_Direct(*PropertyValue, ValuePtr, Template, 0);
            if (Result == nullptr || *Result != 0)
            {
                OutError = FString::Printf(
                    TEXT("Failed to parse value '%s' for property '%s'"),
                    *PropertyValue, *PropertyName);
                return true;
            }
        }

        // ⑥ 编译 + 脏标记（模板属性变化 → 后续实例化组件跟随模板）
        FKismetEditorUtilities::CompileBlueprint(Blueprint);
        Blueprint->MarkPackageDirty();
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);

        OutError = TEXT("");
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Variables Read (T-20260823-UE5BRIDGE-CPP-APIS 子项2) ----------
// 背景：T-20260820-UE5BRIDGE-LEFTOVER-TAIL 遗留②——UE5.1 Python 绑定无
// new_variables 属性（get_editor_property protected 拒绝）→ read_variables
// 假阳性/降级。路径 B 本方法在 C++ 侧反射读取变量清单（C++ 反射不受访问
// 控制限制），Python read_variables 优先调用本 API。
namespace
{
    // FEdGraphPinType → 类型字符串（对齐旧 Python new_variables type_name 语义：
    // Boolean/Integer/Float/Double/String/Text/Name/Vector/Rotator/Transform...）
    FString VariablePinTypeToString(const FEdGraphPinType& PinType)
    {
        const FName Category = PinType.PinCategory;
        if (Category == UEdGraphSchema_K2::PC_Boolean) return TEXT("Boolean");
        if (Category == UEdGraphSchema_K2::PC_Int) return TEXT("Integer");
        if (Category == UEdGraphSchema_K2::PC_Byte)
        {
            if (PinType.PinSubCategoryObject.IsValid() && PinType.PinSubCategoryObject->IsA(UEnum::StaticClass()))
            {
                return PinType.PinSubCategoryObject->GetName();
            }
            return TEXT("Byte");
        }
        if (Category == UEdGraphSchema_K2::PC_Real)
        {
            return (PinType.PinSubCategory == UEdGraphSchema_K2::PC_Float)
                ? TEXT("Float") : TEXT("Double");
        }
        if (Category == UEdGraphSchema_K2::PC_String) return TEXT("String");
        if (Category == UEdGraphSchema_K2::PC_Text) return TEXT("Text");
        if (Category == UEdGraphSchema_K2::PC_Name) return TEXT("Name");
        if (Category == UEdGraphSchema_K2::PC_Struct)
        {
            if (PinType.PinSubCategoryObject.IsValid())
            {
                return PinType.PinSubCategoryObject->GetName();
            }
            return TEXT("Struct");
        }
        if (Category == UEdGraphSchema_K2::PC_Object)
        {
            if (PinType.PinSubCategoryObject.IsValid())
            {
                return FString::Printf(TEXT("Object(%s)"), *PinType.PinSubCategoryObject->GetName());
            }
            return TEXT("Object");
        }
        if (Category == UEdGraphSchema_K2::PC_Class)
        {
            if (PinType.PinSubCategoryObject.IsValid())
            {
                return FString::Printf(TEXT("Class(%s)"), *PinType.PinSubCategoryObject->GetName());
            }
            return TEXT("Class");
        }
        if (Category == UEdGraphSchema_K2::PC_Interface) return TEXT("Interface");
        if (Category == UEdGraphSchema_K2::PC_Enum)
        {
            if (PinType.PinSubCategoryObject.IsValid())
            {
                return PinType.PinSubCategoryObject->GetName();
            }
            return TEXT("Enum");
        }
        return Category.ToString();
    }

    // FProperty → 类型字符串（ChildProperties 兜底路径）
    FString PropertyToTypeName(const FProperty* Prop)
    {
        if (!Prop) return TEXT("Unknown");
        if (const FBoolProperty* B = CastField<FBoolProperty>(Prop)) return TEXT("Boolean");
        if (const FIntProperty* I = CastField<FIntProperty>(Prop)) return TEXT("Integer");
        if (const FInt64Property* I64 = CastField<FInt64Property>(Prop)) return TEXT("Integer64");
        if (const FUInt32Property* U32 = CastField<FUInt32Property>(Prop)) return TEXT("Integer");
        if (const FByteProperty* Bp = CastField<FByteProperty>(Prop))
        {
            return Bp->Enum ? Bp->Enum->GetName() : TEXT("Byte");
        }
        if (const FFloatProperty* F = CastField<FFloatProperty>(Prop)) return TEXT("Float");
        if (const FDoubleProperty* D = CastField<FDoubleProperty>(Prop)) return TEXT("Double");
        if (const FStrProperty* S = CastField<FStrProperty>(Prop)) return TEXT("String");
        if (const FTextProperty* T = CastField<FTextProperty>(Prop)) return TEXT("Text");
        if (const FNameProperty* N = CastField<FNameProperty>(Prop)) return TEXT("Name");
        if (const FEnumProperty* E = CastField<FEnumProperty>(Prop))
        {
            return E->GetEnum() ? E->GetEnum()->GetName() : TEXT("Enum");
        }
        if (const FStructProperty* SP = CastField<FStructProperty>(Prop))
        {
            if (SP->Struct == TBaseStructure<FVector>::Get()) return TEXT("Vector");
            if (SP->Struct == TBaseStructure<FRotator>::Get()) return TEXT("Rotator");
            if (SP->Struct == TBaseStructure<FTransform>::Get()) return TEXT("Transform");
            return SP->Struct ? SP->Struct->GetName() : TEXT("Struct");
        }
        if (const FObjectPropertyBase* OP = CastField<FObjectPropertyBase>(Prop))
        {
            if (OP->PropertyClass) return FString::Printf(TEXT("Object(%s)"), *OP->PropertyClass->GetName());
            return TEXT("Object");
        }
        if (const FInterfaceProperty* IF = CastField<FInterfaceProperty>(Prop)) return TEXT("Interface");
        if (const FDelegateProperty* DL = CastField<FDelegateProperty>(Prop)) return TEXT("Delegate");
        if (const FMulticastDelegateProperty* MDL = CastField<FMulticastDelegateProperty>(Prop)) return TEXT("MulticastDelegate");
        return Prop->GetClass() ? Prop->GetClass()->GetName() : TEXT("Unknown");
    }
}

TArray<FBPVariableInfo> UBlueprintPythonBridge::ReadVariables(UBlueprint* Blueprint)
{
    TArray<FBPVariableInfo> Result;
    if (!Blueprint) return Result;

    auto Work = [Blueprint]() -> TArray<FBPVariableInfo> {
        TArray<FBPVariableInfo> Out;

        // ① 主路径：UBlueprint::NewVariables（protected UPROPERTY → FindFProperty
        //    运行时反射访问，绕开 C++ 访问控制；Python get_editor_property 因
        //    protected 拒绝访问，C++ 反射无此限制）
        FArrayProperty* ArrProp = FindFProperty<FArrayProperty>(
            UBlueprint::StaticClass(), TEXT("NewVariables"));
        if (ArrProp)
        {
            void* ArrayPtr = ArrProp->ContainerPtrToValuePtr<void>(Blueprint);
            FScriptArrayHelper Helper(ArrProp, ArrayPtr);
            for (int32 i = 0; i < Helper.Num(); ++i)
            {
                const FBPVariableDescription* Desc =
                    reinterpret_cast<const FBPVariableDescription*>(Helper.GetRawPtr(i));
                if (!Desc) continue;
                FBPVariableInfo Info;
                Info.VariableName = Desc->VarName.ToString();
                Info.VariableType = VariablePinTypeToString(Desc->VarType);
                Info.DefaultValue = Desc->DefaultValue;
                Info.Category = Desc->Category.ToString();
                Info.bIsEditable = !(Desc->PropertyFlags & CPF_DisableEditOnInstance);
                Out.Add(Info);
            }
        }

        // ② 兜底：NewVariables 为空（极少数未初始化蓝图）→ GeneratedClass
        //    ChildProperties 遍历编译后属性
        if (Out.Num() == 0 && Blueprint->GeneratedClass)
        {
            for (TFieldIterator<FProperty> It(Blueprint->GeneratedClass, EFieldIteratorFlags::ExcludeSuper); It; ++It)
            {
                FProperty* Prop = *It;
                if (!Prop) continue;
                if (Prop->HasAnyPropertyFlags(CPF_Transient)) continue; // 过滤内部瞬态属性
                FString Name = Prop->GetName();
                if (Name.StartsWith(TEXT("__"))) continue; // 过滤委托等内部属性
                FBPVariableInfo Info;
                Info.VariableName = Name;
                Info.VariableType = PropertyToTypeName(Prop);
                Info.bIsEditable = false;
                Out.Add(Info);
            }
        }

        return Out;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- Connections ----------

TArray<FBPConnectionInfo> UBlueprintPythonBridge::GetNodeConnections(UEdGraph* Graph)
{
    TArray<FBPConnectionInfo> Result;
    if (!Graph) return Result;

    for (UEdGraphNode* Node : Graph->Nodes)
    {
        if (!IsValid(Node)) continue;
        FString NodeTitle = Node->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
        FGuid   NodeGuid  = Node->NodeGuid;

        for (UEdGraphPin* Pin : Node->Pins)
        {
            if (!Pin) continue;
            // Record from Output side (avoid Input side duplicates)
            if (Pin->Direction != EGPD_Output) continue;
            if (Pin->LinkedTo.Num() == 0) continue;

            for (UEdGraphPin* LinkedPin : Pin->LinkedTo)
            {
                UEdGraphNode* TargetNode = LinkedPin->GetOwningNode();
                if (!IsValid(TargetNode)) continue;

                FBPConnectionInfo Info;
                Info.FromNodeTitle    = NodeTitle;
                Info.FromPinName      = Pin->PinName.ToString();
                Info.FromPinDirection = TEXT("Output");
                Info.FromNodeGuid     = NodeGuid;

                Info.ToNodeTitle      = TargetNode->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
                Info.ToPinName        = LinkedPin->PinName.ToString();
                Info.ToPinDirection   = TEXT("Input");
                Info.ToNodeGuid       = TargetNode->NodeGuid;

                Result.Add(Info);
            }
        }
    }

    return Result;
}

// ---------- GameThread Python Execute ----------

void UBlueprintPythonBridge::ExecutePythonOnGameThread(const FString& PythonCode, FString& OutResult)
{
    // T-20260723-READFIX BUG#1: No lambda wrapping; pass multi-statement Python directly
    // Python side handles exceptions and result IO; C++ only handles GameThread dispatch
    FString WrappedCode = FString::Printf(
        TEXT("%s\n"),
        *PythonCode
    );

    TPromise<FString> Promise;
    TFuture<FString> Future = Promise.GetFuture();

    AsyncTask(ENamedThreads::GameThread, [&Promise, WrappedCode]()
    {
        IPythonScriptPlugin* PyPlugin = IPythonScriptPlugin::Get();
        if (PyPlugin)
        {
            PyPlugin->ExecPythonCommand(*WrappedCode);
            Promise.SetValue(TEXT("{\"ok\":true}"));
        }
        else
        {
            Promise.SetValue(TEXT("{\"ok\":false,\"error\":\"PythonScriptPlugin module not loaded\"}"));
        }
    });

    OutResult = Future.Get();
}

TArray<FBPConnectionInfo> UBlueprintPythonBridge::GetNodeConnectionsForNode(
    UEdGraph* Graph, const FBPNodeInfo& NodeInfo)
{
    TArray<FBPConnectionInfo> Result;
    if (!Graph) return Result;

    // Step 1: Find real EdGraphNode by node_guid
    UEdGraphNode* RealNode = nullptr;
    if (NodeInfo.NodeGuid.IsValid())
    {
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (IsValid(N) && N->NodeGuid == NodeInfo.NodeGuid)
            {
                RealNode = N;
                break;
            }
        }
    }
    if (!RealNode)
    {
        for (UEdGraphNode* N : Graph->Nodes)
        {
            if (IsValid(N) && N->GetNodeTitle(ENodeTitleType::FullTitle).ToString().Equals(NodeInfo.NodeTitle, ESearchCase::IgnoreCase))
            {
                RealNode = N;
                break;
            }
        }
    }
    if (!RealNode) return Result;

    // Step 2: Only traverse this node's pins
    FString NodeTitle = RealNode->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
    FGuid   NodeGuid  = RealNode->NodeGuid;

    for (UEdGraphPin* Pin : RealNode->Pins)
    {
        if (!Pin) continue;
        if (Pin->LinkedTo.Num() == 0) continue;

        for (UEdGraphPin* LinkedPin : Pin->LinkedTo)
        {
            UEdGraphNode* TargetNode = LinkedPin->GetOwningNode();
            if (!IsValid(TargetNode)) continue;

            FBPConnectionInfo Info;
            if (Pin->Direction == EGPD_Output)
            {
                Info.FromNodeTitle    = NodeTitle;
                Info.FromPinName      = Pin->PinName.ToString();
                Info.FromPinDirection = TEXT("Output");
                Info.FromNodeGuid     = NodeGuid;
                Info.ToNodeTitle      = TargetNode->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
                Info.ToPinName        = LinkedPin->PinName.ToString();
                Info.ToPinDirection   = TEXT("Input");
                Info.ToNodeGuid       = TargetNode->NodeGuid;
            }
            else
            {
                Info.FromNodeTitle    = TargetNode->GetNodeTitle(ENodeTitleType::FullTitle).ToString();
                Info.FromPinName      = LinkedPin->PinName.ToString();
                Info.FromPinDirection = TEXT("Output");
                Info.FromNodeGuid     = TargetNode->NodeGuid;
                Info.ToNodeTitle      = NodeTitle;
                Info.ToPinName        = Pin->PinName.ToString();
                Info.ToPinDirection   = TEXT("Input");
                Info.ToNodeGuid       = NodeGuid;
            }
            Result.Add(Info);
        }
    }

    return Result;
}

// =====================================================================
// T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-A · 15 个新接口实现
// 规格书（唯一需求源）：
//   default/output/T-20260910-UE5BRIDGE-CAPABILITY-AUDIT/Phase2-补全接口规格书.md
// 设计原则（§0）：AddNodeByClass 通用打底（A1）+ 其余语义薄封装（内部转调 A1，
//   注入节点专属配置后 ReconstructNode 重建引脚）；禁止逐节点硬编码；
//   失败一律 fail-loud（返回 nullptr/false + UE_LOG Warning 写明原因）。
// 建节点统一 5 步（§0.5）：AllocateDefaultPins → CreateNewGuid → Graph->AddNode →
//   设 NodePosX/NodePosY → Graph->NotifyGraphChanged
// 线程：与既有 27 接口同款 GameThread Guard（IsInGameThread 内联 / 否则 TaskGraph 调度）
// =====================================================================

// =====================================================================
// PATCH-B 内部共用工具（不出 UFUNCTION）
//   · BridgeResolveClass    —— 类名（全路径 / 短名）解析
//   · BridgeParsePinType    —— 字符串 → FEdGraphPinType
//   · BridgeCreateNode      —— 统一建节点流程（E1 修正后，含 PostPlacedNewNode）
// E1 依据：UE 官方注释 EdGraphNode.h:796「called just once when a new node is created,
//   before AutowireNewNode or AllocateDefaultPins」；引擎 SpawnNodeFromTemplate /
//   K2Node_CustomEvent::CreateFromFunction（K2Node_CustomEvent.cpp:498-505）实测顺序为
//   NewObject → 设初值 → Graph->AddNode → CreateNewGuid → PostPlacedNewNode →
//   AllocateDefaultPins → 设 NodePosX/Y。Composite 的 BoundGraph 与 MathExpression 的
//   表达式子图均依赖该钩子（K2Node_Composite.cpp:308 在 PostPlacedNewNode 内建图，
//   且需 GetGraph() 非空 → 必须 AddNode 在前）。
// =====================================================================
namespace BlueprintPythonBridgeInternal
{
    static UClass* BridgeResolveClass(const FString& InClassPath)
    {
        if (InClassPath.IsEmpty())
        {
            return nullptr;
        }
        UClass* Found = FindObject<UClass>(nullptr, *InClassPath);
        if (!Found)
        {
            Found = LoadObject<UClass>(nullptr, *InClassPath);
        }
        if (!Found)
        {
            Found = FindFirstObject<UClass>(*InClassPath);
        }
        return Found;
    }

    static UScriptStruct* BridgeResolveStruct(const FString& InStructName)
    {
        if (InStructName.IsEmpty())
        {
            return nullptr;
        }
        UScriptStruct* Found = FindObject<UScriptStruct>(nullptr, *InStructName);
        if (!Found)
        {
            Found = LoadObject<UScriptStruct>(nullptr, *InStructName);
        }
        if (!Found)
        {
            Found = FindFirstObject<UScriptStruct>(*InStructName);
        }
        return Found;
    }

    static UEnum* BridgeResolveEnum(const FString& InEnumName)
    {
        if (InEnumName.IsEmpty())
        {
            return nullptr;
        }
        UEnum* Found = FindObject<UEnum>(nullptr, *InEnumName);
        if (!Found)
        {
            Found = LoadObject<UEnum>(nullptr, *InEnumName);
        }
        if (!Found)
        {
            Found = FindFirstObject<UEnum>(*InEnumName);
        }
        return Found;
    }

    /** 引脚类型字符串语法：
     *   exec | bool | byte | int/int32 | int64 | float/real | double | string | name | text |
     *   wildcard |
     *   object:<ClassPath|短名> | softobject:<...> | class:<...> | softclass:<...> |
     *   interface:<...> | struct:<StructName> | enum:<EnumName>
     */
    static bool BridgeParsePinType(const FString& PinTypeStr, FEdGraphPinType& OutPinType)
    {
        OutPinType = FEdGraphPinType();
        const FString Trimmed = PinTypeStr.TrimStartAndEnd();
        if (Trimmed.IsEmpty())
        {
            return false;
        }

        FString Head = Trimmed;
        FString Arg;
        int32 ColonIdx = INDEX_NONE;
        if (Trimmed.FindChar(TEXT(':'), ColonIdx))
        {
            Head = Trimmed.Left(ColonIdx).TrimStartAndEnd();
            Arg = Trimmed.Mid(ColonIdx + 1).TrimStartAndEnd();
        }
        const FString Lower = Head.ToLower();

        if (Lower == TEXT("exec") || Lower == TEXT("execution"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Exec;
            return true;
        }
        if (Lower == TEXT("bool") || Lower == TEXT("boolean"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Boolean;
            return true;
        }
        if (Lower == TEXT("byte"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Byte;
            return true;
        }
        if (Lower == TEXT("int") || Lower == TEXT("int32") || Lower == TEXT("integer"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Int;
            return true;
        }
        if (Lower == TEXT("int64") || Lower == TEXT("long"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Int64;
            return true;
        }
        if (Lower == TEXT("float") || Lower == TEXT("real"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Real;
            OutPinType.PinSubCategory = UEdGraphSchema_K2::PC_Float;
            return true;
        }
        if (Lower == TEXT("double"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Real;
            OutPinType.PinSubCategory = UEdGraphSchema_K2::PC_Double;
            return true;
        }
        if (Lower == TEXT("string"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_String;
            return true;
        }
        if (Lower == TEXT("name"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Name;
            return true;
        }
        if (Lower == TEXT("text"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Text;
            return true;
        }
        if (Lower == TEXT("wildcard"))
        {
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Wildcard;
            return true;
        }
        if (Lower == TEXT("object") || Lower == TEXT("obj"))
        {
            UClass* Class = BridgeResolveClass(Arg);
            if (!Class)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Object;
            OutPinType.PinSubCategoryObject = Class;
            return true;
        }
        if (Lower == TEXT("softobject"))
        {
            UClass* Class = BridgeResolveClass(Arg);
            if (!Class)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_SoftObject;
            OutPinType.PinSubCategoryObject = Class;
            return true;
        }
        if (Lower == TEXT("class"))
        {
            UClass* Class = BridgeResolveClass(Arg);
            if (!Class)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Class;
            OutPinType.PinSubCategoryObject = Class;
            return true;
        }
        if (Lower == TEXT("softclass"))
        {
            UClass* Class = BridgeResolveClass(Arg);
            if (!Class)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_SoftClass;
            OutPinType.PinSubCategoryObject = Class;
            return true;
        }
        if (Lower == TEXT("interface"))
        {
            UClass* Class = BridgeResolveClass(Arg);
            if (!Class)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Interface;
            OutPinType.PinSubCategoryObject = Class;
            return true;
        }
        if (Lower == TEXT("struct"))
        {
            UScriptStruct* Struct = BridgeResolveStruct(Arg);
            if (!Struct)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Struct;
            OutPinType.PinSubCategoryObject = Struct;
            return true;
        }
        if (Lower == TEXT("enum"))
        {
            UEnum* Enum = BridgeResolveEnum(Arg);
            if (!Enum)
            {
                return false;
            }
            OutPinType.PinCategory = UEdGraphSchema_K2::PC_Byte;
            OutPinType.PinSubCategoryObject = Enum;
            return true;
        }
        return false;
    }

    /** 统一建节点流程（E1 修正版）：AddNode → CreateNewGuid → PostPlacedNewNode →
     *  AllocateDefaultPins → 设坐标 → NotifyGraphChanged。PreGuidSetup 在 CreateNewGuid
     *  之前调用，用于「必须早于 GUID 生成」的初值（如 K2Node_CustomEvent::CustomFunctionName，
     *  依据 K2Node_CustomEvent.cpp:499-504）。 */
    static UEdGraphNode* BridgeCreateNode(UEdGraph* Graph, UClass* NodeClass, int32 PosX, int32 PosY,
        const TFunction<void(UEdGraphNode*)>& PreGuidSetup)
    {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);

        UEdGraphNode* Node = NewObject<UEdGraphNode>(Graph, NodeClass);
        if (!Node)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] BridgeCreateNode FAILED: NewObject returned null for '%s'"),
                NodeClass ? *NodeClass->GetName() : TEXT("<null class>"));
            return nullptr;
        }

        Node->SetFlags(RF_Transactional);
        Graph->Modify();
        Node->Modify();
        // AddNode 必须在 PostPlacedNewNode 之前：Composite::PostPlacedNewNode 依赖 GetGraph() 非空
        Graph->AddNode(Node, false, false);

        if (PreGuidSetup)
        {
            PreGuidSetup(Node);
        }

        Node->CreateNewGuid();
        Node->PostPlacedNewNode();   // E1：新节点一次性初始化钩子
        Node->AllocateDefaultPins();
        Node->NodePosX = PosX;
        Node->NodePosY = PosY;
        Graph->NotifyGraphChanged();

        if (BP)
        {
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        }
        return Node;
    }

    /** 找函数图入口节点（函数图 / 事件分发器签名图 / 宏图均适用） */
    static UK2Node_FunctionEntry* BridgeFindFunctionEntry(UEdGraph* Graph)
    {
        if (!Graph)
        {
            return nullptr;
        }
        for (UEdGraphNode* Node : Graph->Nodes)
        {
            if (UK2Node_FunctionEntry* Entry = Cast<UK2Node_FunctionEntry>(Node))
            {
                return Entry;
            }
        }
        return nullptr;
    }

    /** 反射写 FName 属性（用于 protected UPROPERTY，如 UK2Node_BaseAsyncTask::ProxyFactoryFunctionName） */
    static bool BridgeSetFNameProperty(UObject* Object, const FName PropertyName, const FName Value)
    {
        if (!Object)
        {
            return false;
        }
        FNameProperty* Prop = FindFProperty<FNameProperty>(Object->GetClass(), PropertyName);
        if (!Prop)
        {
            return false;
        }
        Object->Modify();
        Prop->SetPropertyValue_InContainer(Object, Value);
        return true;
    }

    /** 反射写对象属性（UClass* / UObject*） */
    static bool BridgeSetObjectProperty(UObject* Object, const FName PropertyName, UObject* Value)
    {
        if (!Object)
        {
            return false;
        }
        FObjectPropertyBase* Prop = FindFProperty<FObjectPropertyBase>(Object->GetClass(), PropertyName);
        if (!Prop)
        {
            return false;
        }
        Object->Modify();
        Prop->SetObjectPropertyValue_InContainer(Object, Value);
        return true;
    }
}

// ---------- A1 · 通用建节点（A 批地基，解锁全部具体节点类型）----------
UEdGraphNode* UBlueprintPythonBridge::AddNodeByClass(
    UEdGraph* Graph, TSubclassOf<UEdGraphNode> NodeClass, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddNodeByClass FAILED: Graph is null"));
        return nullptr;
    }
    if (!NodeClass)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddNodeByClass FAILED: NodeClass is null"));
        return nullptr;
    }
    // §0.6：目标类空指针/非法类一律早退，不得进 NewObject
    if (!NodeClass->IsChildOf(UEdGraphNode::StaticClass()))
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddNodeByClass FAILED: '%s' is not a UEdGraphNode subclass"),
            *NodeClass->GetName());
        return nullptr;
    }
    // NewObject 对抽象类会触发引擎 checkf，这里提前 fail-loud
    if (NodeClass->HasAnyClassFlags(CLASS_Abstract))
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddNodeByClass FAILED: '%s' is abstract"),
            *NodeClass->GetName());
        return nullptr;
    }

    auto Work = [Graph, NodeClass, PosX, PosY]() -> UEdGraphNode* {
        // 统一流程（E1 修正版）：AddNode → CreateNewGuid → PostPlacedNewNode →
        //   AllocateDefaultPins → 设 NodePosX/NodePosY → NotifyGraphChanged
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, NodeClass, PosX, PosY, TFunction<void(UEdGraphNode*)>());
        if (!Node)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddNodeByClass FAILED: node creation returned null for '%s'"),
                *NodeClass->GetName());
            return nullptr;
        }

        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddNodeByClass OK: %s @ (%d,%d)"),
            *NodeClass->GetName(), PosX, PosY);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- A2 · 设置节点坐标 ----------
bool UBlueprintPythonBridge::SetNodePosition(UEdGraphNode* Node, int32 X, int32 Y)
{
    if (!Node)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodePosition FAILED: Node is null"));
        return false;
    }

    auto Work = [Node, X, Y]() -> bool {
        UEdGraph* Graph = Node->GetGraph();
        if (!Graph)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodePosition FAILED: node has no owning graph"));
            return false;
        }
        Graph->Modify();
        Node->Modify();
        Node->NodePosX = X;
        Node->NodePosY = Y;
        Graph->NotifyGraphChanged();
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- A3 · 按 NodeGuid 精确查找节点（解决重名节点接错线）----------
UEdGraphNode* UBlueprintPythonBridge::GetNodeByGuid(UEdGraph* Graph, const FString& GuidStr)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] GetNodeByGuid FAILED: Graph is null"));
        return nullptr;
    }

    FGuid TargetGuid;
    if (!FGuid::Parse(GuidStr, TargetGuid))
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] GetNodeByGuid FAILED: invalid GUID string '%s'"), *GuidStr);
        return nullptr;
    }
    if (!TargetGuid.IsValid())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] GetNodeByGuid FAILED: GUID '%s' is not valid"), *GuidStr);
        return nullptr;
    }

    for (UEdGraphNode* Node : Graph->Nodes)
    {
        if (IsValid(Node) && Node->NodeGuid == TargetGuid)
        {
            return Node;
        }
    }

    UE_LOG(LogTemp, Warning, TEXT("[BPBridge] GetNodeByGuid FAILED: no node with GUID %s in graph '%s'"),
        *TargetGuid.ToString(), *Graph->GetName());
    return nullptr;
}

// ---------- A4 · 反射写节点属性 ----------
bool UBlueprintPythonBridge::SetNodeProperty(
    UEdGraphNode* Node, const FString& PropertyName, const FString& Value)
{
    if (!Node)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodeProperty FAILED: Node is null"));
        return false;
    }
    if (PropertyName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodeProperty FAILED: PropertyName is empty"));
        return false;
    }

    auto Work = [Node, PropertyName, Value]() -> bool {
        FProperty* Prop = FindFProperty<FProperty>(Node->GetClass(), FName(*PropertyName));
        if (!Prop)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodeProperty FAILED: property '%s' not found on node class '%s'"),
                *PropertyName, *Node->GetClass()->GetName());
            return false;
        }

        Node->Modify();
        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Node);
        if (!Prop->ImportText_Direct(*Value, ValuePtr, Node, PPF_None))
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetNodeProperty FAILED: cannot import '%s' into '%s' (%s)"),
                *Value, *PropertyName, *Prop->GetCPPType());
            return false;
        }

        if (UEdGraph* Graph = Node->GetGraph())
        {
            Graph->NotifyGraphChanged();
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] SetNodeProperty OK: %s = '%s'"),
            *PropertyName, *Value);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B1 · 变量 Get/Set 节点 ----------
// 规格书要点：取 UBlueprint（Graph->GetOuter）→ FindMemberVariableGuidByName 校验成员
//   → UK2Node_VariableGet/Set → SetFromProperty（禁止手拼 FMemberReference）
UEdGraphNode* UBlueprintPythonBridge::AddVariableNode(
    UEdGraph* Graph, const FString& VarName, bool bIsSetter, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddVariableNode FAILED: Graph is null"));
        return nullptr;
    }
    if (VarName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddVariableNode FAILED: VarName is empty"));
        return nullptr;
    }

    auto Work = [Graph, VarName, bIsSetter, PosX, PosY]() -> UEdGraphNode* {
        // ① UBlueprint（Graph->GetOuter），失败兜底 FindBlueprintForGraph
        UBlueprint* BP = Cast<UBlueprint>(Graph->GetOuter());
        if (!BP)
        {
            BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        }
        if (!BP)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddVariableNode FAILED: no UBlueprint owner for graph '%s'"),
                *Graph->GetName());
            return nullptr;
        }

        // ② 必须是本蓝图 NewVariables 声明的成员变量（FBlueprintEditorUtils 权威查询）
        const FGuid VarGuid = FBlueprintEditorUtils::FindMemberVariableGuidByName(BP, FName(*VarName));
        if (!VarGuid.IsValid())
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddVariableNode FAILED: variable '%s' is not a member variable of blueprint '%s'"),
                *VarName, *BP->GetName());
            return nullptr;
        }

        // ③ 取变量 FProperty（SetFromProperty 的必需入参）
        UClass* SelfClass = BP->SkeletonGeneratedClass ? BP->SkeletonGeneratedClass : BP->GeneratedClass;
        if (!SelfClass)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddVariableNode FAILED: blueprint '%s' has no generated class"),
                *BP->GetName());
            return nullptr;
        }
        FProperty* VarProp = FindFProperty<FProperty>(SelfClass, FName(*VarName));
        if (!VarProp)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddVariableNode FAILED: FProperty for variable '%s' not found on class '%s'"),
                *VarName, *SelfClass->GetName());
            return nullptr;
        }

        // ④ 通用层建节点（A1 打底）
        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(Graph,
            bIsSetter ? UK2Node_VariableSet::StaticClass() : UK2Node_VariableGet::StaticClass(),
            PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_Variable* VarNode = Cast<UK2Node_Variable>(Node);
        if (!VarNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddVariableNode FAILED: node is not a UK2Node_Variable"));
            return nullptr;
        }

        // ⑤ SetFromProperty（规格书：不要手拼 FMemberReference）+ 重建引脚
        VarNode->SetFromProperty(VarProp, /*bSelfContext=*/true, SelfClass);
        VarNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddVariableNode OK: %s '%s' on '%s'"),
            bIsSetter ? TEXT("Set") : TEXT("Get"), *VarName, *SelfClass->GetName());
        return VarNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B2 · 动态 Cast 节点 ----------
// 规格书要点：TargetType = LoadClass<UClass>(TargetClassName)；
//   类名支持 /Script/Engine.Actor 与 /Game/Blueprints/BP_X.BP_X_C 两种形式
UEdGraphNode* UBlueprintPythonBridge::AddCastNode(
    UEdGraph* Graph, const FString& TargetClassName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCastNode FAILED: Graph is null"));
        return nullptr;
    }
    if (TargetClassName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCastNode FAILED: TargetClassName is empty"));
        return nullptr;
    }

    auto Work = [Graph, TargetClassName, PosX, PosY]() -> UEdGraphNode* {
        UClass* TargetClass = FindObject<UClass>(nullptr, *TargetClassName);
        if (!TargetClass)
        {
            TargetClass = LoadObject<UClass>(nullptr, *TargetClassName);
        }
        if (!TargetClass)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCastNode FAILED: cannot resolve class '%s'"),
                *TargetClassName);
            return nullptr;
        }

        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
            Graph, UK2Node_DynamicCast::StaticClass(), PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_DynamicCast* CastNode = Cast<UK2Node_DynamicCast>(Node);
        if (!CastNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCastNode FAILED: node is not a UK2Node_DynamicCast"));
            return nullptr;
        }

        CastNode->TargetType = TargetClass;
        CastNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddCastNode OK: cast to '%s'"), *TargetClass->GetName());
        return CastNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B3 · 标准宏实例节点 ----------
namespace
{
    // 在「引擎标准宏库 + 所有已加载蓝图」中按名查找宏图（宏图宿主蓝图的 Outer 名为 StandardMacros）
    UEdGraph* FindMacroGraphByName(const FString& MacroName)
    {
        const FName MacroFName(*MacroName);
        TArray<UBlueprint*> Candidates;

        // ① 引擎内置标准宏库资产
        static const TCHAR* StdMacroLibPath =
            TEXT("/Engine/EditorBlueprintResources/StandardMacros.StandardMacros");
        UBlueprint* StdMacroLib = FindObject<UBlueprint>(nullptr, StdMacroLibPath);
        if (!StdMacroLib)
        {
            StdMacroLib = LoadObject<UBlueprint>(nullptr, StdMacroLibPath);
        }
        if (StdMacroLib)
        {
            Candidates.Add(StdMacroLib);
        }

        // ② 兜底：所有已加载且含宏图的蓝图（用户宏库同样命中）
        for (TObjectIterator<UBlueprint> It; It; ++It)
        {
            UBlueprint* BP = *It;
            if (BP && BP->MacroGraphs.Num() > 0 && !Candidates.Contains(BP))
            {
                Candidates.Add(BP);
            }
        }

        for (UBlueprint* BP : Candidates)
        {
            for (UEdGraph* Graph : BP->MacroGraphs)
            {
                if (Graph && Graph->GetFName() == MacroFName)
                {
                    return Graph;
                }
            }
        }
        return nullptr;
    }
}

UEdGraphNode* UBlueprintPythonBridge::AddMacroNode(
    UEdGraph* Graph, const FString& MacroName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddMacroNode FAILED: Graph is null"));
        return nullptr;
    }
    if (MacroName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddMacroNode FAILED: MacroName is empty"));
        return nullptr;
    }

    auto Work = [Graph, MacroName, PosX, PosY]() -> UEdGraphNode* {
        UEdGraph* MacroGraph = FindMacroGraphByName(MacroName);
        if (!MacroGraph)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddMacroNode FAILED: standard macro '%s' not found "
                     "(expect e.g. ForEachLoop / ForEachLoopWithBreak / WhileLoop / IsValid / Gate / FlipFlop; "
                     "NOTE[E5]: DoOnce / MultiGate / Sequence are standalone K2 node classes in UE5.1, NOT macros)"),
                *MacroName);
            return nullptr;
        }

        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
            Graph, UK2Node_MacroInstance::StaticClass(), PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_MacroInstance* MacroNode = Cast<UK2Node_MacroInstance>(Node);
        if (!MacroNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddMacroNode FAILED: node is not a UK2Node_MacroInstance"));
            return nullptr;
        }

        // 先绑宏图，再 ReconstructNode 按宏图签名重建引脚
        MacroNode->SetMacroGraph(MacroGraph);
        MacroNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddMacroNode OK: '%s' (macro graph from '%s')"),
            *MacroName, *MacroGraph->GetOuter()->GetName());
        return MacroNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B4 · 按键事件节点 ----------
// 规格书要点：KeyName 形如 W / A / SpaceBar / LeftMouseButton / RightMouseButton → FKey(FName)
UEdGraphNode* UBlueprintPythonBridge::AddInputKeyEventNode(
    UEdGraph* Graph, const FString& KeyName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputKeyEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (KeyName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputKeyEventNode FAILED: KeyName is empty"));
        return nullptr;
    }

    auto Work = [Graph, KeyName, PosX, PosY]() -> UEdGraphNode* {
        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
            Graph, UK2Node_InputKey::StaticClass(), PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_InputKey* KeyNode = Cast<UK2Node_InputKey>(Node);
        if (!KeyNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputKeyEventNode FAILED: node is not a UK2Node_InputKey"));
            return nullptr;
        }

        KeyNode->InputKey = FKey(FName(*KeyName));
        if (!KeyNode->InputKey.IsValid())
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddInputKeyEventNode FAILED: '%s' is not a registered key name "
                     "(expect e.g. W / A / SpaceBar / LeftMouseButton / RightMouseButton)"),
                *KeyName);
            return nullptr;
        }

        KeyNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddInputKeyEventNode OK: key '%s'"),
            *KeyNode->InputKey.GetFName().ToString());
        return KeyNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B5 · InputAction 事件节点（Legacy Input Action）----------
// ⚠️ Enhanced Input 走本接口需实测（规格书 B5）；不可用时由调用方降级为 B4
UEdGraphNode* UBlueprintPythonBridge::AddInputActionEventNode(
    UEdGraph* Graph, const FString& ActionName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputActionEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (ActionName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputActionEventNode FAILED: ActionName is empty"));
        return nullptr;
    }

    auto Work = [Graph, ActionName, PosX, PosY]() -> UEdGraphNode* {
        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
            Graph, UK2Node_InputAction::StaticClass(), PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_InputAction* ActionNode = Cast<UK2Node_InputAction>(Node);
        if (!ActionNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddInputActionEventNode FAILED: node is not a UK2Node_InputAction"));
            return nullptr;
        }

        ActionNode->InputActionName = FName(*ActionName);
        ActionNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddInputActionEventNode OK: action '%s'"), *ActionName);
        return ActionNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B6 · 组件委托绑定事件节点（门开关碰撞事件依赖）----------
// 规格书要点：UK2Node_ComponentBoundEvent + 委托初始化 + ComponentPropertyName；
//   注：UE5.1 实际 API 为 InitializeComponentBoundEventParams(FObjectProperty*, FMulticastDelegateProperty*)
UEdGraphNode* UBlueprintPythonBridge::AddComponentBoundEventNode(
    UEdGraph* Graph, const FString& ComponentName, const FString& DelegateName,
    int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddComponentBoundEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (ComponentName.IsEmpty() || DelegateName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddComponentBoundEventNode FAILED: ComponentName and DelegateName must be non-empty"));
        return nullptr;
    }

    auto Work = [Graph, ComponentName, DelegateName, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: no owner blueprint for graph '%s'"),
                *Graph->GetName());
            return nullptr;
        }

        UClass* BPClass = BP->SkeletonGeneratedClass ? BP->SkeletonGeneratedClass : BP->GeneratedClass;
        if (!BPClass)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: blueprint '%s' has no generated class"),
                *BP->GetName());
            return nullptr;
        }

        // ① 组件名必须是蓝图类上的 FObjectProperty（SCS 组件编译后落到 GeneratedClass）
        FObjectProperty* CompProp = FindFProperty<FObjectProperty>(BPClass, FName(*ComponentName));
        if (!CompProp)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: component property '%s' not found on class '%s' "
                     "(component added to SCS? blueprint compiled?)"),
                *ComponentName, *BPClass->GetName());
            return nullptr;
        }

        // ② 委托必须是组件类上的 FMulticastDelegateProperty
        UClass* CompClass = CompProp->PropertyClass;
        if (!CompClass)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: component property '%s' has no PropertyClass"),
                *ComponentName);
            return nullptr;
        }
        FMulticastDelegateProperty* DelegateProp =
            FindFProperty<FMulticastDelegateProperty>(CompClass, FName(*DelegateName));
        if (!DelegateProp)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: delegate '%s' not found on component class '%s'"),
                *DelegateName, *CompClass->GetName());
            return nullptr;
        }

        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
            Graph, UK2Node_ComponentBoundEvent::StaticClass(), PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_ComponentBoundEvent* EventNode = Cast<UK2Node_ComponentBoundEvent>(Node);
        if (!EventNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddComponentBoundEventNode FAILED: node is not a UK2Node_ComponentBoundEvent"));
            return nullptr;
        }

        // ③ 委托初始化（写入 ComponentPropertyName / DelegatePropertyName / DelegateOwnerClass）
        EventNode->InitializeComponentBoundEventParams(CompProp, DelegateProp);
        EventNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddComponentBoundEventNode OK: '%s.%s'"),
            *ComponentName, *DelegateName);
        return EventNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B7 · Switch 节点 ----------
// 规格书要点：TypeKind ∈ {int, string, name, enum} → 对应 UK2Node_Switch*；
//   enum 时指定枚举（UE5.1 实际 API 为 UK2Node_SwitchEnum::SetEnum(UEnum*)）
UEdGraphNode* UBlueprintPythonBridge::AddSwitchNode(
    UEdGraph* Graph, const FString& TypeKind, const FString& EnumOrTypeName,
    int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddSwitchNode FAILED: Graph is null"));
        return nullptr;
    }
    if (TypeKind.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddSwitchNode FAILED: TypeKind is empty"));
        return nullptr;
    }

    auto Work = [Graph, TypeKind, EnumOrTypeName, PosX, PosY]() -> UEdGraphNode* {
        // E7：Switch 节点建出后必须补 case 执行引脚，否则只有 Default 出口（节点不可用）。
        //   int/string/name → UK2Node_Switch::AddPinToSwitchNode()
        //     （K2Node_Switch.cpp:256 内部 CreatePin(EGPD_Output, PC_Exec, GetUniquePinName())）
        //   enum → SetEnum() 会填充 EnumEntries/EnumFriendlyNames（K2Node_SwitchEnum.cpp:39-65），
        //     随后 ReconstructNode → AllocateDefaultPins 按 EnumEntries 生成 case 引脚
        //     （K2Node_SwitchEnum.cpp:169-171）
        auto AddCasePin = [](UEdGraphNode* SwitchNode) -> UEdGraphNode* {
            if (!SwitchNode)
            {
                return nullptr;
            }
            UK2Node_Switch* SwitchBase = Cast<UK2Node_Switch>(SwitchNode);
            if (!SwitchBase)
            {
                return SwitchNode;
            }
            SwitchBase->AddPinToSwitchNode();
            if (UEdGraph* OwningGraph = SwitchNode->GetGraph())
            {
                OwningGraph->NotifyGraphChanged();
            }
            return SwitchNode;
        };

        if (TypeKind.Equals(TEXT("int"), ESearchCase::IgnoreCase))
        {
            return AddCasePin(UBlueprintPythonBridge::AddNodeByClass(
                Graph, UK2Node_SwitchInteger::StaticClass(), PosX, PosY));
        }
        if (TypeKind.Equals(TEXT("string"), ESearchCase::IgnoreCase))
        {
            return AddCasePin(UBlueprintPythonBridge::AddNodeByClass(
                Graph, UK2Node_SwitchString::StaticClass(), PosX, PosY));
        }
        if (TypeKind.Equals(TEXT("name"), ESearchCase::IgnoreCase))
        {
            return AddCasePin(UBlueprintPythonBridge::AddNodeByClass(
                Graph, UK2Node_SwitchName::StaticClass(), PosX, PosY));
        }
        if (TypeKind.Equals(TEXT("enum"), ESearchCase::IgnoreCase))
        {
            if (EnumOrTypeName.IsEmpty())
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddSwitchNode FAILED: TypeKind 'enum' requires EnumOrTypeName"));
                return nullptr;
            }

            UEnum* Enum = FindObject<UEnum>(nullptr, *EnumOrTypeName);
            if (!Enum)
            {
                Enum = LoadObject<UEnum>(nullptr, *EnumOrTypeName);
            }
            if (!Enum)
            {
                Enum = FindFirstObject<UEnum>(*EnumOrTypeName);
            }
            if (!Enum)
            {
                UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddSwitchNode FAILED: cannot resolve enum '%s'"),
                    *EnumOrTypeName);
                return nullptr;
            }

            UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(
                Graph, UK2Node_SwitchEnum::StaticClass(), PosX, PosY);
            if (!Node)
            {
                return nullptr;
            }

            UK2Node_SwitchEnum* EnumNode = Cast<UK2Node_SwitchEnum>(Node);
            if (!EnumNode)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddSwitchNode FAILED: node is not a UK2Node_SwitchEnum"));
                return nullptr;
            }

            // E7 + MinimalAPI 规避（实测 LNK2019）：
            //   UK2Node_SwitchEnum 声明为 UCLASS(MinimalAPI)（K2Node_SwitchEnum.h:28），
            //   SetEnum / ReloadEnum 均未导出（?SetEnum@UK2Node_SwitchEnum@@QEAAXPEAVUEnum@@@Z 外部无法解析）
            //   → 改走反射写 public UPROPERTY Enum（字段级，无需导出符号），
            //     再走虚函数 ReconstructNode()；其内部 CreateCasePins() 会自行 SetEnum(Enum)
            //     并按 EnumEntries 生成 case 引脚（K2Node_SwitchEnum.cpp:156-171，模块内调用不受限）
            if (!BlueprintPythonBridgeInternal::BridgeSetObjectProperty(
                    EnumNode, FName(TEXT("Enum")), Enum))
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddSwitchNode FAILED: cannot set 'Enum' property on UK2Node_SwitchEnum"));
                return nullptr;
            }
            EnumNode->ReconstructNode();
            UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddSwitchNode OK: enum '%s'"), *Enum->GetName());
            return EnumNode;
        }

        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddSwitchNode FAILED: unknown TypeKind '%s' (expect int|string|name|enum)"),
            *TypeKind);
        return nullptr;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B8 · 结构体 Make/Break 节点 ----------
// 规格书要点：UK2Node_BreakStruct / UK2Node_MakeStruct + StructType = FindObject<UScriptStruct>；
//   支持 Vector / Rotator / Transform / HitResult（短名走 FindFirstObject 兜底）
UEdGraphNode* UBlueprintPythonBridge::AddStructNode(
    UEdGraph* Graph, const FString& StructName, bool bIsBreak, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddStructNode FAILED: Graph is null"));
        return nullptr;
    }
    if (StructName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddStructNode FAILED: StructName is empty"));
        return nullptr;
    }

    auto Work = [Graph, StructName, bIsBreak, PosX, PosY]() -> UEdGraphNode* {
        // ① 全路径（/Script/CoreUObject.Vector）→ ② 短名全局查找（Vector / Rotator / ...）
        UScriptStruct* Struct = FindObject<UScriptStruct>(nullptr, *StructName);
        if (!Struct)
        {
            Struct = LoadObject<UScriptStruct>(nullptr, *StructName);
        }
        if (!Struct)
        {
            Struct = FindFirstObject<UScriptStruct>(*StructName);
        }
        if (!Struct)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddStructNode FAILED: cannot resolve struct '%s' "
                     "(expect e.g. Vector / Rotator / Transform / HitResult or a full path)"),
                *StructName);
            return nullptr;
        }

        UEdGraphNode* Node = UBlueprintPythonBridge::AddNodeByClass(Graph,
            bIsBreak ? UK2Node_BreakStruct::StaticClass() : UK2Node_MakeStruct::StaticClass(),
            PosX, PosY);
        if (!Node)
        {
            return nullptr;
        }

        UK2Node_StructOperation* StructNode = Cast<UK2Node_StructOperation>(Node);
        if (!StructNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddStructNode FAILED: node is not a UK2Node_StructOperation"));
            return nullptr;
        }

        StructNode->StructType = Struct;
        StructNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddStructNode OK: %s struct '%s'"),
            bIsBreak ? TEXT("Break") : TEXT("Make"), *Struct->GetName());
        return StructNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// =====================================================================
// C 批 · 蓝图 / 类层（3）
// =====================================================================

// ---------- C1 · 写类默认值（解「PC 无实例 → 视角/鼠标光标只能改默认值」）----------
bool UBlueprintPythonBridge::SetClassDefault(
    UBlueprint* Blueprint, const FString& PropertyName, const FString& Value)
{
    if (!Blueprint)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetClassDefault FAILED: Blueprint is null"));
        return false;
    }
    if (PropertyName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] SetClassDefault FAILED: PropertyName is empty"));
        return false;
    }

    auto Work = [Blueprint, PropertyName, Value]() -> bool {
        UClass* BPClass = Blueprint->GeneratedClass;
        if (!BPClass)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] SetClassDefault FAILED: blueprint '%s' has no GeneratedClass (compiled?)"),
                *Blueprint->GetName());
            return false;
        }

        UObject* CDO = BPClass->GetDefaultObject();
        if (!CDO)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] SetClassDefault FAILED: class '%s' has no default object"),
                *BPClass->GetName());
            return false;
        }

        FProperty* Prop = FindFProperty<FProperty>(BPClass, FName(*PropertyName));
        if (!Prop)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] SetClassDefault FAILED: property '%s' not found on class '%s'"),
                *PropertyName, *BPClass->GetName());
            return false;
        }

        // 修改前 Modify() 保护事务/GC
        CDO->Modify();
        Blueprint->Modify();

        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(CDO);
        if (!Prop->ImportText_Direct(*Value, ValuePtr, CDO, PPF_None))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] SetClassDefault FAILED: cannot import '%s' into '%s' (%s)"),
                *Value, *PropertyName, *Prop->GetCPPType());
            return false;
        }

        Blueprint->MarkPackageDirty();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] SetClassDefault OK: %s.%s = '%s'"),
            *BPClass->GetName(), *PropertyName, *Value);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- C2 · 移除 SCS 组件 ----------
// 语义对齐裁决 A（同 AddComponentToBlueprint / SetComponentProperty）：
//   ''=成功 / 非空 str=失败+错误文本
//   ⚠️ UE5.1 Python 反射（PyGenUtil.cpp PackReturnValues）：bool 主返回 + FString& out 时
//   bool=false → Python 拿到 None（OutError 被吞）；因此需透传错误文本的失败一律
//   return true + OutError 非空。
bool UBlueprintPythonBridge::RemoveComponent(
    UBlueprint* Blueprint, const FString& ComponentName, FString& OutError)
{
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }

    auto Work = [Blueprint, ComponentName, &OutError]() -> bool {
        if (ComponentName.IsEmpty())
        {
            OutError = TEXT("ComponentName is empty");
            return true;
        }

        // ① 取 SCS（与 AddComponentToBlueprint 同源路径）
        USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript;
        if (!SCS)
        {
            OutError = TEXT("Blueprint has no SimpleConstructionScript");
            return true;
        }

        // ② 按 VariableName 精确定位组件节点
        USCS_Node* TargetNode = nullptr;
        const TArray<USCS_Node*>& Nodes = SCS->GetAllNodes();
        for (USCS_Node* Node : Nodes)
        {
            if (Node && Node->GetVariableName() == FName(*ComponentName))
            {
                TargetNode = Node;
                break;
            }
        }
        if (!TargetNode)
        {
            OutError = FString::Printf(
                TEXT("Component '%s' not found in SimpleConstructionScript"), *ComponentName);
            return true;
        }

        // ③ 移除 + 结构化修改 + 脏标记
        Blueprint->Modify();
        SCS->Modify();
        SCS->RemoveNode(TargetNode);
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        Blueprint->MarkPackageDirty();

        OutError = TEXT("");
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] RemoveComponent OK: '%s'"), *ComponentName);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- C3 · 蓝图实现新接口 ----------
bool UBlueprintPythonBridge::AddImplementedInterface(
    UBlueprint* Blueprint, const FString& InterfaceClassPath)
{
    if (!Blueprint)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddImplementedInterface FAILED: Blueprint is null"));
        return false;
    }
    if (InterfaceClassPath.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddImplementedInterface FAILED: InterfaceClassPath is empty"));
        return false;
    }

    auto Work = [Blueprint, InterfaceClassPath]() -> bool {
        const bool bImplemented =
            FBlueprintEditorUtils::ImplementNewInterface(Blueprint, FName(*InterfaceClassPath));
        if (!bImplemented)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddImplementedInterface FAILED: cannot implement '%s' on blueprint '%s'"),
                *InterfaceClassPath, *Blueprint->GetName());
            return false;
        }

        Blueprint->MarkPackageDirty();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddImplementedInterface OK: '%s' on '%s'"),
            *InterfaceClassPath, *Blueprint->GetName());
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// =====================================================================
// T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-B · 必须 8 接口实现
// 权威合并清单：default/output/T-20260910-UE5BRIDGE-CAPABILITY-AUDIT/Phase2d-最终清单-合并审查版.md §2
//   B-1 AddPinToNode / B-2 RemovePinFromNode / B-3 TryNodeAddInputPin（动态引脚三件套）
//   B-4 AddCommentNode / B-5 AddCustomEventNode
//   B-6a AddFunctionPinEntry / B-6b AddLocalVariable
//   B-7 AddEventDispatcher
// 硬纠错落点：E1 → AddNodeByClass/BridgeCreateNode（PostPlacedNewNode 已插入）
//   E4 → 本块用 CreatePin/RemovePin（UE5.1 无 UEdGraphNode::AddPin）
//   E7 → AddSwitchNode 内补 AddPinToSwitchNode（见上文 B7）
// =====================================================================

// ---------- B-1 · 动态引脚：通用加引脚（EdGraphNode.h:559 CreatePin）----------
// ⚠️ 签名偏离规格：UEdGraphPin 非 UHT 反射类型（UHT 报 Unable to find 'struct' UEdGraphPin），
//   故对外改为 bool + OutPinName 回填；内部仍使用 CreatePin 返回的 UEdGraphPin*。
bool UBlueprintPythonBridge::AddPinToNode(
    UEdGraphNode* Node, const FString& Direction, const FString& PinTypeStr,
    const FString& PinName, FString& OutPinName)
{
    OutPinName.Empty();
    if (!Node)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddPinToNode FAILED: Node is null"));
        return false;
    }
    if (PinName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddPinToNode FAILED: PinName is empty"));
        return false;
    }

    EEdGraphPinDirection PinDirection;
    if (Direction.Equals(TEXT("input"), ESearchCase::IgnoreCase)
        || Direction.Equals(TEXT("in"), ESearchCase::IgnoreCase))
    {
        PinDirection = EGPD_Input;
    }
    else if (Direction.Equals(TEXT("output"), ESearchCase::IgnoreCase)
        || Direction.Equals(TEXT("out"), ESearchCase::IgnoreCase))
    {
        PinDirection = EGPD_Output;
    }
    else
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddPinToNode FAILED: unknown Direction '%s' (expect input|output)"), *Direction);
        return false;
    }

    FEdGraphPinType PinType;
    if (!BlueprintPythonBridgeInternal::BridgeParsePinType(PinTypeStr, PinType))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddPinToNode FAILED: cannot parse PinTypeStr '%s' "
                 "(expect exec|bool|int|int64|float|double|string|name|text|wildcard|"
                 "object:<Class>|class:<Class>|softobject:<Class>|softclass:<Class>|"
                 "interface:<Class>|struct:<Struct>|enum:<Enum>)"),
            *PinTypeStr);
        return false;
    }

    auto Work = [Node, PinDirection, PinType, PinName, &OutPinName]() -> bool {
        const FName PinFName(*PinName);
        if (Node->FindPin(PinFName))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddPinToNode FAILED: pin '%s' already exists on node '%s'"),
                *PinName, *Node->GetClass()->GetName());
            return false;
        }

        Node->Modify();
        UEdGraphPin* NewPin = Node->CreatePin(PinDirection, PinType, PinFName);
        if (!NewPin)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddPinToNode FAILED: CreatePin returned null for '%s'"), *PinName);
            return false;
        }
        if (UEdGraph* Graph = Node->GetGraph())
        {
            Graph->NotifyGraphChanged();
        }
        OutPinName = NewPin->PinName.ToString();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddPinToNode OK: '%s' on '%s'"),
            *OutPinName, *Node->GetClass()->GetName());
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-2 · 动态引脚：按名移除（EdGraphNode.h:622 RemovePin）----------
bool UBlueprintPythonBridge::RemovePinFromNode(UEdGraphNode* Node, const FString& PinName)
{
    if (!Node)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] RemovePinFromNode FAILED: Node is null"));
        return false;
    }
    if (PinName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] RemovePinFromNode FAILED: PinName is empty"));
        return false;
    }

    auto Work = [Node, PinName]() -> bool {
        UEdGraphPin* Pin = Node->FindPin(FName(*PinName));
        if (!Pin)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] RemovePinFromNode FAILED: no pin '%s' on node '%s'"),
                *PinName, *Node->GetClass()->GetName());
            return false;
        }
        Node->Modify();
        if (!Node->RemovePin(Pin))
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] RemovePinFromNode FAILED: RemovePin refused '%s'"), *PinName);
            return false;
        }
        if (UEdGraph* Graph = Node->GetGraph())
        {
            Graph->NotifyGraphChanged();
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] RemovePinFromNode OK: '%s'"), *PinName);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-3 · 动态引脚：节点语义层加输入引脚（IK2Node_AddPinInterface）----------
bool UBlueprintPythonBridge::TryNodeAddInputPin(UEdGraphNode* Node)
{
    if (!Node)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] TryNodeAddInputPin FAILED: Node is null"));
        return false;
    }

    auto Work = [Node]() -> bool {
        IK2Node_AddPinInterface* AddPinInterface = Cast<IK2Node_AddPinInterface>(Node);
        if (!AddPinInterface)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] TryNodeAddInputPin FAILED: node class '%s' does not implement "
                     "IK2Node_AddPinInterface"),
                *Node->GetClass()->GetName());
            return false;
        }
        if (!AddPinInterface->CanAddPin())
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] TryNodeAddInputPin FAILED: CanAddPin() returned false for '%s'"),
                *Node->GetClass()->GetName());
            return false;
        }
        Node->Modify();
        AddPinInterface->AddInputPin();
        if (UEdGraph* Graph = Node->GetGraph())
        {
            Graph->NotifyGraphChanged();
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] TryNodeAddInputPin OK: '%s'"), *Node->GetClass()->GetName());
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-4 · 注释框（UEdGraphNode_Comment · UnrealEd 模块 · Python 不可达）----------
UEdGraphNode* UBlueprintPythonBridge::AddCommentNode(
    UEdGraph* Graph, const FString& Text, int32 PosX, int32 PosY, int32 Width, int32 Height)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCommentNode FAILED: Graph is null"));
        return nullptr;
    }

    auto Work = [Graph, Text, PosX, PosY, Width, Height]() -> UEdGraphNode* {
        // D-2 修复（T-20260910-UE5BRIDGE-D1D2-FIX）：
        //   引擎 UEdGraphNode_Comment::PostPlacedNewNode()（EdGraphNode_Comment.cpp:95）会
        //   强制把 NodeComment 重置为默认文本，并同步覆盖 CommentColor（:92）。
        //   而 BridgeCreateNode 的 PreGuidSetup 钩子在该 PostPlacedNewNode 之前执行
        //   → 原本写在 lambda 里的注释文本/颜色全部被覆盖丢失（现象：回读标题仍是「注释」）。
        //   修法：注释框属性一律改到「节点创建完成（PostPlacedNewNode 之后）」再落笔。
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UEdGraphNode_Comment::StaticClass(), PosX, PosY,
            TFunction<void(UEdGraphNode*)>());
        if (!Node)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCommentNode FAILED: node creation returned null"));
            return nullptr;
        }

        // D-2：节点创建完成后写入注释框属性（此时不会再被 PostPlacedNewNode 覆盖）
        Node->Modify();
        Node->NodeComment = Text;
        if (Width > 0)
        {
            Node->NodeWidth = Width;
        }
        if (Height > 0)
        {
            Node->NodeHeight = Height;
        }
        if (UEdGraphNode_Comment* CommentNode = Cast<UEdGraphNode_Comment>(Node))
        {
            CommentNode->CommentColor = FLinearColor(0.10f, 0.10f, 0.30f, 0.55f);
            CommentNode->CommentDepth = -1;
        }
        Graph->NotifyGraphChanged();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddCommentNode OK @ (%d,%d) size (%d,%d)"),
            PosX, PosY, Width, Height);
        return Node;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-5 · 自定义事件（先设 CustomFunctionName 再走 E1 顺序）----------
// 依据：K2Node_CustomEvent.cpp:498-505 —— NewObject → CustomFunctionName → AddNode →
//   CreateNewGuid → PostPlacedNewNode → AllocateDefaultPins
UEdGraphNode* UBlueprintPythonBridge::AddCustomEventNode(
    UEdGraph* Graph, const FString& EventName, int32 PosX, int32 PosY, const FString& SignatureFuncName)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCustomEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (EventName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCustomEventNode FAILED: EventName is empty"));
        return nullptr;
    }

    auto Work = [Graph, EventName, PosX, PosY, SignatureFuncName]() -> UEdGraphNode* {
        UFunction* Signature = nullptr;
        if (!SignatureFuncName.IsEmpty())
        {
            Signature = FindObject<UFunction>(nullptr, *SignatureFuncName);
            if (!Signature)
            {
                Signature = FindFirstObject<UFunction>(*SignatureFuncName);
            }
            if (!Signature)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddCustomEventNode FAILED: cannot resolve signature function '%s'"),
                    *SignatureFuncName);
                return nullptr;
            }
        }

        const FName EventFName(*EventName);
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_CustomEvent::StaticClass(), PosX, PosY,
            [EventFName](UEdGraphNode* NewNode)
            {
                if (UK2Node_CustomEvent* CustomEventNode = Cast<UK2Node_CustomEvent>(NewNode))
                {
                    CustomEventNode->CustomFunctionName = EventFName;
                }
            });
        if (!Node)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCustomEventNode FAILED: node creation returned null"));
            return nullptr;
        }

        UK2Node_CustomEvent* CustomEventNode = Cast<UK2Node_CustomEvent>(Node);
        if (!CustomEventNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddCustomEventNode FAILED: node is not a UK2Node_CustomEvent"));
            return nullptr;
        }

        if (Signature)
        {
            CustomEventNode->SetDelegateSignature(Signature);
            CustomEventNode->ReconstructNode();
        }

        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddCustomEventNode OK: '%s'"), *EventName);
        return CustomEventNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-6a · 函数图参数引脚 ----------
// 方向语义（K2Node_FunctionEntry.cpp:430 CanCreateUserDefinedPin 拒绝 EGPD_Input；
//   K2Node_EditablePinBase.cpp:494 入口 = EGPD_Output、返回 = EGPD_Input）
bool UBlueprintPythonBridge::AddFunctionPinEntry(
    UEdGraph* Graph, const FString& PinName, const FString& PinTypeStr, bool bIsInput)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddFunctionPinEntry FAILED: Graph is null"));
        return false;
    }
    if (PinName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddFunctionPinEntry FAILED: PinName is empty"));
        return false;
    }

    FEdGraphPinType PinType;
    if (!BlueprintPythonBridgeInternal::BridgeParsePinType(PinTypeStr, PinType))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddFunctionPinEntry FAILED: cannot parse PinTypeStr '%s'"), *PinTypeStr);
        return false;
    }

    auto Work = [Graph, PinName, PinType, bIsInput]() -> bool {
        UK2Node_FunctionEntry* EntryNode = BlueprintPythonBridgeInternal::BridgeFindFunctionEntry(Graph);
        if (!EntryNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddFunctionPinEntry FAILED: graph '%s' has no UK2Node_FunctionEntry "
                     "(use add_function_graph first)"),
                *Graph->GetName());
            return false;
        }

        UK2Node_EditablePinBase* TargetNode = EntryNode;
        const EEdGraphPinDirection RequestedDirection = bIsInput ? EGPD_Output : EGPD_Input;
        if (!bIsInput)
        {
            TargetNode = FBlueprintEditorUtils::FindOrCreateFunctionResultNode(EntryNode);
            if (!TargetNode)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddFunctionPinEntry FAILED: cannot find/create function result node"));
                return false;
            }
        }

        FText OutErrorMessage;
        if (!TargetNode->CanCreateUserDefinedPin(PinType, RequestedDirection, OutErrorMessage))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddFunctionPinEntry FAILED: '%s' rejected: %s"),
                *PinName, *OutErrorMessage.ToString());
            return false;
        }

        TargetNode->Modify();
        UEdGraphPin* NewPin = TargetNode->CreateUserDefinedPin(FName(*PinName), PinType, RequestedDirection);
        if (!NewPin)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddFunctionPinEntry FAILED: CreateUserDefinedPin returned null for '%s'"), *PinName);
            return false;
        }
        TargetNode->ReconstructNode();
        Graph->NotifyGraphChanged();
        if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph))
        {
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddFunctionPinEntry OK: '%s' (%s)"),
            *PinName, bIsInput ? TEXT("input") : TEXT("output"));
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-6b · 函数图局部变量（UK2Node_FunctionEntry::LocalVariables）----------
bool UBlueprintPythonBridge::AddLocalVariable(
    UEdGraph* Graph, const FString& VarName, const FString& VarType)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddLocalVariable FAILED: Graph is null"));
        return false;
    }
    if (VarName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddLocalVariable FAILED: VarName is empty"));
        return false;
    }

    FEdGraphPinType PinType;
    if (!BlueprintPythonBridgeInternal::BridgeParsePinType(VarType, PinType))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddLocalVariable FAILED: cannot parse VarType '%s'"), *VarType);
        return false;
    }

    auto Work = [Graph, VarName, VarType, PinType]() -> bool {
        UK2Node_FunctionEntry* EntryNode = BlueprintPythonBridgeInternal::BridgeFindFunctionEntry(Graph);
        if (!EntryNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddLocalVariable FAILED: graph '%s' has no UK2Node_FunctionEntry"), *Graph->GetName());
            return false;
        }

        const FName VarFName(*VarName);
        for (const FBPVariableDescription& Existing : EntryNode->LocalVariables)
        {
            if (Existing.VarName == VarFName)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddLocalVariable FAILED: local variable '%s' already exists"), *VarName);
                return false;
            }
        }

        FBPVariableDescription NewVariable;
        NewVariable.VarName = VarFName;
        NewVariable.VarGuid = FGuid::NewGuid();
        NewVariable.VarType = PinType;
        NewVariable.FriendlyName = VarName;
        NewVariable.Category = FText::FromString(TEXT("Default"));
        NewVariable.PropertyFlags = CPF_Edit | CPF_ZeroConstructor;

        EntryNode->Modify();
        EntryNode->LocalVariables.Add(NewVariable);
        EntryNode->ReconstructNode();
        Graph->NotifyGraphChanged();
        if (UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph))
        {
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddLocalVariable OK: '%s' (%s)"), *VarName, *VarType);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- B-7 · 事件分发器（DelegateSignatureGraphs）----------
// 引擎无公开静态 API（Phase2c §3.4 负面证据）；此处按 FBlueprintEditor::OnAddNewDelegate
//   （BlueprintEditor.cpp:9216-9262）的官方路径自建：
//   AddMemberVariable(PC_MCDelegate) → CreateNewGraph → CreateDefaultNodesForGraph →
//   CreateFunctionGraphTerminators → AddExtraFunctionFlags → MarkFunctionEntryAsEditable →
//   DelegateSignatureGraphs.Add
bool UBlueprintPythonBridge::AddEventDispatcher(
    UBlueprint* Blueprint, const FString& DelegateName, FString& OutError)
{
    OutError.Empty();
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }
    if (DelegateName.IsEmpty())
    {
        OutError = TEXT("DelegateName is empty");
        return false;
    }

    auto Work = [Blueprint, DelegateName, &OutError]() -> bool {
        const FName DelegateFName(*DelegateName);
        for (UEdGraph* Existing : Blueprint->DelegateSignatureGraphs)
        {
            if (Existing && Existing->GetFName() == DelegateFName)
            {
                OutError = FString::Printf(TEXT("event dispatcher '%s' already exists on blueprint '%s'"),
                    *DelegateName, *Blueprint->GetName());
                return false;
            }
        }

        const UEdGraphSchema_K2* K2Schema = GetDefault<UEdGraphSchema_K2>();
        if (!K2Schema)
        {
            OutError = TEXT("UEdGraphSchema_K2 CDO is unavailable");
            return false;
        }

        Blueprint->Modify();

        FEdGraphPinType DelegateType;
        DelegateType.PinCategory = UEdGraphSchema_K2::PC_MCDelegate;
        if (!FBlueprintEditorUtils::AddMemberVariable(Blueprint, DelegateFName, DelegateType))
        {
            OutError = FString::Printf(TEXT("AddMemberVariable(PC_MCDelegate) failed for '%s'"), *DelegateName);
            return false;
        }

        UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(
            Blueprint, DelegateFName, UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
        if (!NewGraph)
        {
            FBlueprintEditorUtils::RemoveMemberVariable(Blueprint, DelegateFName);
            OutError = TEXT("CreateNewGraph failed for the delegate signature graph");
            return false;
        }

        NewGraph->bEditable = false;
        K2Schema->CreateDefaultNodesForGraph(*NewGraph);
        K2Schema->CreateFunctionGraphTerminators(*NewGraph, (UClass*)nullptr);
        K2Schema->AddExtraFunctionFlags(NewGraph, (FUNC_BlueprintCallable | FUNC_BlueprintEvent | FUNC_Public));
        K2Schema->MarkFunctionEntryAsEditable(NewGraph, true);

        Blueprint->DelegateSignatureGraphs.Add(NewGraph);
        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        Blueprint->MarkPackageDirty();

        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddEventDispatcher OK: '%s' on '%s'"),
            *DelegateName, *Blueprint->GetName());
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// =====================================================================
// T-20260910-UE5BRIDGE-CAPABILITY-AUDIT-PATCH-B · 建议 9 接口实现
// 权威合并清单 §4：轴输入事件族 / ActorBoundEvent / CreateDelegate / Timeline /
//   ClassDynamicCast / Composite / MathExpression / AsyncAction / EnumLiteral
// =====================================================================

// ---------- S1 · 轴输入事件（Legacy 轴映射 WASD 场景）----------
UEdGraphNode* UBlueprintPythonBridge::AddInputAxisEventNode(
    UEdGraph* Graph, const FString& AxisName, bool bIsKeyAxis, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputAxisEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (AxisName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddInputAxisEventNode FAILED: AxisName is empty"));
        return nullptr;
    }

    auto Work = [Graph, AxisName, bIsKeyAxis, PosX, PosY]() -> UEdGraphNode* {
        if (bIsKeyAxis)
        {
            UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
                Graph, UK2Node_InputAxisKeyEvent::StaticClass(), PosX, PosY,
                TFunction<void(UEdGraphNode*)>());
            UK2Node_InputAxisKeyEvent* AxisKeyNode = Cast<UK2Node_InputAxisKeyEvent>(Node);
            if (!AxisKeyNode)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddInputAxisEventNode FAILED: node is not a UK2Node_InputAxisKeyEvent"));
                return nullptr;
            }
            // 注意：不能写成 const FKey AxisKey(FName(*AxisName)); —— 会被解析成函数声明
            //   （most vexing parse：FKey AxisKey(FName* AxisName)），必须拆两步
            const FName AxisFName(*AxisName);
            const FKey AxisKey(AxisFName);
            if (!AxisKey.IsValid())
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddInputAxisEventNode FAILED: '%s' is not a registered key name"), *AxisName);
                return nullptr;
            }
            AxisKeyNode->Initialize(AxisKey);
            AxisKeyNode->ReconstructNode();
            UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddInputAxisEventNode OK: axis key '%s'"), *AxisName);
            return AxisKeyNode;
        }

        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_InputAxisEvent::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_InputAxisEvent* AxisNode = Cast<UK2Node_InputAxisEvent>(Node);
        if (!AxisNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddInputAxisEventNode FAILED: node is not a UK2Node_InputAxisEvent"));
            return nullptr;
        }
        AxisNode->Initialize(FName(*AxisName));
        AxisNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddInputAxisEventNode OK: axis '%s'"), *AxisName);
        return AxisNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S2 · 绑定关卡 Actor 实例事件 ----------
UEdGraphNode* UBlueprintPythonBridge::AddActorBoundEventNode(
    UEdGraph* Graph, const FString& ActorName, const FString& DelegateName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddActorBoundEventNode FAILED: Graph is null"));
        return nullptr;
    }
    if (ActorName.IsEmpty() || DelegateName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[BPBridge] AddActorBoundEventNode FAILED: ActorName and DelegateName must be non-empty"));
        return nullptr;
    }

    auto Work = [Graph, ActorName, DelegateName, PosX, PosY]() -> UEdGraphNode* {
        AActor* TargetActor = FindObject<AActor>(nullptr, *ActorName);
        if (!TargetActor && GEditor)
        {
            if (UWorld* World = GEditor->GetEditorWorldContext().World())
            {
                for (TActorIterator<AActor> It(World); It; ++It)
                {
                    AActor* Candidate = *It;
                    if (Candidate
                        && (Candidate->GetName() == ActorName || Candidate->GetActorNameOrLabel() == ActorName))
                    {
                        TargetActor = Candidate;
                        break;
                    }
                }
            }
        }
        if (!TargetActor)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddActorBoundEventNode FAILED: cannot find level actor '%s' "
                     "(open the level containing it, or pass its full object path)"),
                *ActorName);
            return nullptr;
        }

        FMulticastDelegateProperty* DelegateProperty =
            FindFProperty<FMulticastDelegateProperty>(TargetActor->GetClass(), FName(*DelegateName));
        if (!DelegateProperty)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddActorBoundEventNode FAILED: delegate '%s' not found on actor class '%s'"),
                *DelegateName, *TargetActor->GetClass()->GetName());
            return nullptr;
        }

        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_ActorBoundEvent::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_ActorBoundEvent* EventNode = Cast<UK2Node_ActorBoundEvent>(Node);
        if (!EventNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddActorBoundEventNode FAILED: node is not a UK2Node_ActorBoundEvent"));
            return nullptr;
        }
        EventNode->InitializeActorBoundEventParams(TargetActor, DelegateProperty);
        EventNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddActorBoundEventNode OK: '%s.%s'"),
            *ActorName, *DelegateName);
        return EventNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S3 · CreateDelegate（Bind Event to X）----------
UEdGraphNode* UBlueprintPythonBridge::AddCreateDelegateNode(
    UEdGraph* Graph, const FString& FunctionName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCreateDelegateNode FAILED: Graph is null"));
        return nullptr;
    }
    if (FunctionName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCreateDelegateNode FAILED: FunctionName is empty"));
        return nullptr;
    }

    auto Work = [Graph, FunctionName, PosX, PosY]() -> UEdGraphNode* {
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_CreateDelegate::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_CreateDelegate* DelegateNode = Cast<UK2Node_CreateDelegate>(Node);
        if (!DelegateNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddCreateDelegateNode FAILED: node is not a UK2Node_CreateDelegate"));
            return nullptr;
        }
        DelegateNode->SetFunction(FName(*FunctionName));
        DelegateNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddCreateDelegateNode OK: function '%s'"), *FunctionName);
        return DelegateNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S4 · Timeline 节点 ----------
UEdGraphNode* UBlueprintPythonBridge::AddTimelineNode(
    UEdGraph* Graph, const FString& TimelineName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddTimelineNode FAILED: Graph is null"));
        return nullptr;
    }

    auto Work = [Graph, TimelineName, PosX, PosY]() -> UEdGraphNode* {
        UBlueprint* BP = FBlueprintEditorUtils::FindBlueprintForGraph(Graph);
        if (!BP)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddTimelineNode FAILED: no owner blueprint for graph"));
            return nullptr;
        }

        const FName TimelineFName = TimelineName.IsEmpty()
            ? FBlueprintEditorUtils::FindUniqueTimelineName(BP)
            : FName(*TimelineName);

        // 必须先建 UTimelineTemplate：UK2Node_Timeline::AllocateDefaultPins 依赖
        //   Blueprint->FindTimelineTemplateByVariableName(TimelineName)（K2Node_Timeline.cpp:143）
        UTimelineTemplate* TimelineTemplate = FBlueprintEditorUtils::AddNewTimeline(BP, TimelineFName);
        if (!TimelineTemplate)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddTimelineNode FAILED: AddNewTimeline('%s') returned null"), *TimelineFName.ToString());
            return nullptr;
        }

        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_Timeline::StaticClass(), PosX, PosY,
            [TimelineFName](UEdGraphNode* NewNode)
            {
                if (UK2Node_Timeline* TimelineNode = Cast<UK2Node_Timeline>(NewNode))
                {
                    TimelineNode->TimelineName = TimelineFName;
                }
            });
        UK2Node_Timeline* TimelineNode = Cast<UK2Node_Timeline>(Node);
        if (!TimelineNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddTimelineNode FAILED: node is not a UK2Node_Timeline"));
            return nullptr;
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddTimelineNode OK: '%s'"), *TimelineFName.ToString());
        return TimelineNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S5 · Class 动态 Cast（对类引用 Cast）----------
UEdGraphNode* UBlueprintPythonBridge::AddClassCastNode(
    UEdGraph* Graph, const FString& TargetClassName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddClassCastNode FAILED: Graph is null"));
        return nullptr;
    }
    if (TargetClassName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddClassCastNode FAILED: TargetClassName is empty"));
        return nullptr;
    }

    auto Work = [Graph, TargetClassName, PosX, PosY]() -> UEdGraphNode* {
        UClass* TargetClass = BlueprintPythonBridgeInternal::BridgeResolveClass(TargetClassName);
        if (!TargetClass)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddClassCastNode FAILED: cannot resolve class '%s'"), *TargetClassName);
            return nullptr;
        }

        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_ClassDynamicCast::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_ClassDynamicCast* CastNode = Cast<UK2Node_ClassDynamicCast>(Node);
        if (!CastNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddClassCastNode FAILED: node is not a UK2Node_ClassDynamicCast"));
            return nullptr;
        }
        CastNode->TargetType = TargetClass;
        CastNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddClassCastNode OK: class cast to '%s'"), *TargetClass->GetName());
        return CastNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S6 · 折叠图 Composite ----------
UEdGraphNode* UBlueprintPythonBridge::AddCompositeNode(UEdGraph* Graph, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCompositeNode FAILED: Graph is null"));
        return nullptr;
    }

    auto Work = [Graph, PosX, PosY]() -> UEdGraphNode* {
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_Composite::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_Composite* CompositeNode = Cast<UK2Node_Composite>(Node);
        if (!CompositeNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddCompositeNode FAILED: node is not a UK2Node_Composite"));
            return nullptr;
        }
        if (!CompositeNode->BoundGraph)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddCompositeNode FAILED: BoundGraph was not created "
                     "(PostPlacedNewNode did not run with a valid owning graph)"));
            return nullptr;
        }
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddCompositeNode OK: bound graph '%s'"),
            *CompositeNode->BoundGraph->GetName());
        return CompositeNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S7 · 数学表达式节点 ----------
UEdGraphNode* UBlueprintPythonBridge::AddMathExpressionNode(
    UEdGraph* Graph, const FString& Expression, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddMathExpressionNode FAILED: Graph is null"));
        return nullptr;
    }
    if (Expression.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddMathExpressionNode FAILED: Expression is empty"));
        return nullptr;
    }

    auto Work = [Graph, Expression, PosX, PosY]() -> UEdGraphNode* {
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_MathExpression::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_MathExpression* MathNode = Cast<UK2Node_MathExpression>(Node);
        if (!MathNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddMathExpressionNode FAILED: node is not a UK2Node_MathExpression"));
            return nullptr;
        }
        // K2Node_MathExpression.cpp:2847 —— ReconstructNode 内部调 RebuildExpression(Expression)
        MathNode->Expression = Expression;
        MathNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddMathExpressionNode OK: '%s'"), *Expression);
        return MathNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S8 · 异步任务节点（ProxyFactory* 为 protected → 反射写入）----------
UEdGraphNode* UBlueprintPythonBridge::AddAsyncActionNode(
    UEdGraph* Graph, const FString& FactoryFunctionName, const FString& FactoryClassName,
    const FString& ActivateFunctionName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddAsyncActionNode FAILED: Graph is null"));
        return nullptr;
    }
    if (FactoryFunctionName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddAsyncActionNode FAILED: FactoryFunctionName is empty"));
        return nullptr;
    }

    auto Work = [Graph, FactoryFunctionName, FactoryClassName, ActivateFunctionName, PosX, PosY]() -> UEdGraphNode* {
        UClass* FactoryClass = nullptr;
        if (!FactoryClassName.IsEmpty())
        {
            FactoryClass = BlueprintPythonBridgeInternal::BridgeResolveClass(FactoryClassName);
            if (!FactoryClass)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddAsyncActionNode FAILED: cannot resolve factory class '%s'"), *FactoryClassName);
                return nullptr;
            }
        }

        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_AsyncAction::StaticClass(), PosX, PosY, TFunction<void(UEdGraphNode*)>());
        UK2Node_AsyncAction* AsyncNode = Cast<UK2Node_AsyncAction>(Node);
        if (!AsyncNode)
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddAsyncActionNode FAILED: node is not a UK2Node_AsyncAction"));
            return nullptr;
        }

        // UK2Node_BaseAsyncTask.h:94-109 —— 四个字段为 protected UPROPERTY，走反射写入
        if (!BlueprintPythonBridgeInternal::BridgeSetFNameProperty(
                AsyncNode, FName(TEXT("ProxyFactoryFunctionName")), FName(*FactoryFunctionName)))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddAsyncActionNode FAILED: cannot set ProxyFactoryFunctionName"));
            return nullptr;
        }
        if (FactoryClass
            && !BlueprintPythonBridgeInternal::BridgeSetObjectProperty(
                AsyncNode, FName(TEXT("ProxyFactoryClass")), FactoryClass))
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddAsyncActionNode FAILED: cannot set ProxyFactoryClass"));
            return nullptr;
        }
        if (!ActivateFunctionName.IsEmpty())
        {
            BlueprintPythonBridgeInternal::BridgeSetFNameProperty(
                AsyncNode, FName(TEXT("ProxyActivateFunctionName")), FName(*ActivateFunctionName));
        }

        AsyncNode->ReconstructNode();
        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddAsyncActionNode OK: factory '%s'"), *FactoryFunctionName);
        return AsyncNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ---------- S9 · 枚举字面量节点 ----------
UEdGraphNode* UBlueprintPythonBridge::AddEnumLiteralNode(
    UEdGraph* Graph, const FString& EnumName, const FString& ValueName, int32 PosX, int32 PosY)
{
    if (!Graph)
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddEnumLiteralNode FAILED: Graph is null"));
        return nullptr;
    }
    if (EnumName.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("[BPBridge] AddEnumLiteralNode FAILED: EnumName is empty"));
        return nullptr;
    }

    auto Work = [Graph, EnumName, ValueName, PosX, PosY]() -> UEdGraphNode* {
        UEnum* Enum = BlueprintPythonBridgeInternal::BridgeResolveEnum(EnumName);
        if (!Enum)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddEnumLiteralNode FAILED: cannot resolve enum '%s'"), *EnumName);
            return nullptr;
        }

        // Enum 在 CreateNewGuid/AllocateDefaultPins 之前写入，保证引脚按枚举生成
        UEdGraphNode* Node = BlueprintPythonBridgeInternal::BridgeCreateNode(
            Graph, UK2Node_EnumLiteral::StaticClass(), PosX, PosY,
            [Enum](UEdGraphNode* NewNode)
            {
                if (UK2Node_EnumLiteral* EnumLiteralNode = Cast<UK2Node_EnumLiteral>(NewNode))
                {
                    EnumLiteralNode->Enum = Enum;
                }
            });
        UK2Node_EnumLiteral* EnumLiteralNode = Cast<UK2Node_EnumLiteral>(Node);
        if (!EnumLiteralNode)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] AddEnumLiteralNode FAILED: node is not a UK2Node_EnumLiteral"));
            return nullptr;
        }
        EnumLiteralNode->ReconstructNode();

        if (!ValueName.IsEmpty())
        {
            UEdGraphPin* EnumPin = EnumLiteralNode->FindPin(UK2Node_EnumLiteral::GetEnumInputPinName());
            if (!EnumPin)
            {
                UE_LOG(LogTemp, Warning,
                    TEXT("[BPBridge] AddEnumLiteralNode FAILED: enum input pin '%s' not found"),
                    *UK2Node_EnumLiteral::GetEnumInputPinName().ToString());
                return nullptr;
            }
            EnumPin->DefaultValue = ValueName;
        }

        UE_LOG(LogTemp, Log, TEXT("[BPBridge] AddEnumLiteralNode OK: enum '%s' value '%s'"),
            *Enum->GetName(), *ValueName);
        return EnumLiteralNode;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

// ============================================================
//  UMG 控件蓝图（v3.0 · T-20260918-V3-UMG）
//  既有 K2Node/EdGraph 能力对 WidgetBlueprint 的 EventGraph 直接复用；
//  本组补：资产创建 / 控件树 / 控件属性。
//  反射语义对齐裁决 A：'' = 成功 / 非空 str = 失败+错误文本。
// ============================================================

UBlueprint* UBlueprintPythonBridge::CreateWidgetBlueprint(
    const FString& AssetPath, const FString& AssetName)
{
    auto Work = [AssetPath, AssetName]() -> UBlueprint* {
        FString PackageName = AssetPath;
        if (!PackageName.StartsWith(TEXT("/Game")))
            PackageName = TEXT("/Game/") + PackageName;
        while (PackageName.EndsWith(TEXT("/")))
            PackageName = PackageName.LeftChop(1);
        PackageName = PackageName / AssetName;

        if (FindObject<UBlueprint>(nullptr, *PackageName))
        {
            UE_LOG(LogTemp, Warning, TEXT("[BPBridge] Widget BP '%s' already exists (full path)"), *PackageName);
            return nullptr;
        }
        UPackage* ExistingPackage = FindPackage(nullptr, *PackageName);
        if (ExistingPackage)
        {
            if (UBlueprint* Existing = FindObject<UBlueprint>(ExistingPackage, *AssetName))
                return Existing;
        }
        UPackage* Package = CreatePackage(*PackageName);
        if (!Package)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] Failed to create package '%s'"), *PackageName);
            return nullptr;
        }
        if (UBlueprint* Leaked = FindObject<UBlueprint>(Package, *AssetName))
            return Leaked;

        UBlueprint* BP = FKismetEditorUtilities::CreateBlueprint(
            UUserWidget::StaticClass(),
            Package,
            *AssetName,
            BPTYPE_Normal,
            UWidgetBlueprint::StaticClass(),
            UWidgetBlueprintGeneratedClass::StaticClass(),
            NAME_None
        );
        if (!BP)
        {
            UE_LOG(LogTemp, Error, TEXT("[BPBridge] Failed to create widget blueprint '%s'"), *AssetName);
            return nullptr;
        }
        // WidgetTree 兜底（部分引擎路径下 CreateBlueprint 不自动建树）
        UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(BP);
        if (WBP && !WBP->WidgetTree)
        {
            WBP->WidgetTree = NewObject<UWidgetTree>(WBP, TEXT("WidgetTree"), RF_Transactional);
        }

        FAssetRegistryModule::AssetCreated(BP);
        FKismetEditorUtilities::CompileBlueprint(BP);
        BP->MarkPackageDirty();
        FBlueprintEditorUtils::MarkBlueprintAsModified(BP);

        FString File;
        // v3.2 5.8 兼容：DoesPackageExist 的 Guid 重载已移除；SavePackage 改为
        // FSavePackageArgs 现代重载（5.1 亦支持该重载 → 单套代码跨版本）
        if (FPackageName::DoesPackageExist(Package->GetName(), &File))
        {
            FSavePackageArgs SaveArgs;
            SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
            SaveArgs.Error = GError;
            SaveArgs.SaveFlags = SAVE_NoError;
            UPackage::SavePackage(Package, BP, *File, SaveArgs);
        }
        return BP;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

bool UBlueprintPythonBridge::AddWidgetToTree(
    UBlueprint* Blueprint, const FString& ParentWidgetName,
    const FString& WidgetClassPath, const FString& WidgetName,
    FString& OutError)
{
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }
    auto Work = [Blueprint, ParentWidgetName, WidgetClassPath, WidgetName, &OutError]() -> bool {
        UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(Blueprint);
        if (!WBP || !WBP->WidgetTree)
        {
            OutError = TEXT("Not a WidgetBlueprint or WidgetTree missing");
            return true;
        }
        if (WidgetName.IsEmpty())
        {
            OutError = TEXT("WidgetName must be non-empty");
            return true;
        }
        if (WBP->WidgetTree->FindWidget(FName(*WidgetName)))
        {
            OutError = FString::Printf(TEXT("Widget '%s' already exists in WidgetTree"), *WidgetName);
            return true;
        }
        // 类路径解析：全路径优先；短名补 /Script/UMG. 前缀兜底
        UClass* WidgetClass = LoadClass<UWidget>(nullptr, *WidgetClassPath);
        if (!WidgetClass && !WidgetClassPath.Contains(TEXT("/")))
        {
            const FString Full = FString::Printf(TEXT("/Script/UMG.%s"), *WidgetClassPath);
            WidgetClass = LoadClass<UWidget>(nullptr, *Full);
        }
        if (!WidgetClass)
        {
            OutError = FString::Printf(
                TEXT("WidgetClass not resolvable: '%s'（示例 /Script/UMG.TextBlock 或短名 TextBlock；"
                     "仅 UWidget 派生类可用）"), *WidgetClassPath);
            return true;
        }

        UWidget* NewWidget = NewObject<UWidget>(WBP->WidgetTree, WidgetClass, FName(*WidgetName), RF_Transactional);
        if (!NewWidget)
        {
            OutError = TEXT("NewObject<UWidget> failed");
            return true;
        }

        if (ParentWidgetName.IsEmpty())
        {
            if (WBP->WidgetTree->RootWidget)
            {
                OutError = FString::Printf(
                    TEXT("RootWidget already exists ('%s') — pass ParentWidgetName to attach under a panel"),
                    *WBP->WidgetTree->RootWidget->GetName());
                return true;
            }
            WBP->WidgetTree->RootWidget = NewWidget;
        }
        else
        {
            UWidget* Parent = WBP->WidgetTree->FindWidget(FName(*ParentWidgetName));
            UPanelWidget* Panel = Cast<UPanelWidget>(Parent);
            if (!Panel)
            {
                OutError = FString::Printf(
                    TEXT("Parent '%s' not found or not a panel widget (需要 UPanelWidget 派生："
                         "CanvasPanel/VerticalBox/HorizontalBox/Overlay 等）"), *ParentWidgetName);
                return true;
            }
            if (!Panel->AddChild(NewWidget))
            {
                OutError = FString::Printf(TEXT("Panel->AddChild failed for '%s'"), *WidgetName);
                return true;
            }
        }

        FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

bool UBlueprintPythonBridge::SetWidgetProperty(
    UBlueprint* Blueprint, const FString& WidgetName,
    const FString& PropertyName, const FString& Value,
    FString& OutError)
{
    if (!Blueprint)
    {
        OutError = TEXT("Blueprint is null");
        return false;
    }
    auto Work = [Blueprint, WidgetName, PropertyName, Value, &OutError]() -> bool {
        UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(Blueprint);
        if (!WBP || !WBP->WidgetTree)
        {
            OutError = TEXT("Not a WidgetBlueprint or WidgetTree missing");
            return true;
        }
        if (WidgetName.IsEmpty() || PropertyName.IsEmpty())
        {
            OutError = TEXT("WidgetName and PropertyName must be non-empty");
            return true;
        }
        UWidget* W = WBP->WidgetTree->FindWidget(FName(*WidgetName));
        if (!W)
        {
            OutError = FString::Printf(TEXT("Widget '%s' not found in WidgetTree"), *WidgetName);
            return true;
        }
        FProperty* Prop = FindFProperty<FProperty>(W->GetClass(), FName(*PropertyName));
        if (!Prop)
        {
            OutError = FString::Printf(TEXT("Property '%s' not found on widget '%s' (class %s)"),
                *PropertyName, *WidgetName, *W->GetClass()->GetName());
            return true;
        }
        if (!Prop->HasAnyPropertyFlags(CPF_Edit))
        {
            OutError = FString::Printf(TEXT("Property '%s' on widget '%s' is not editable"),
                *PropertyName, *WidgetName);
            return true;
        }
        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(W);
        const TCHAR* Result = Prop->ImportText_Direct(*Value, ValuePtr, W, 0);
        if (Result == nullptr || *Result != 0)
        {
            OutError = FString::Printf(TEXT("Failed to parse value '%s' for property '%s'"),
                *Value, *PropertyName);
            return true;
        }
        FBlueprintEditorUtils::MarkBlueprintAsModified(Blueprint);
        return true;
    };
    if (IsInGameThread()) return Work();
    return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
}

TArray<FBPWidgetInfo> UBlueprintPythonBridge::ReadWidgetTree(UBlueprint* Blueprint)
{
    TArray<FBPWidgetInfo> Out;
    if (!Blueprint)
        return Out;
    auto Work = [Blueprint, &Out]() -> bool {
        UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(Blueprint);
        if (!WBP || !WBP->WidgetTree)
            return false;
        TArray<UWidget*> AllWidgets;
        WBP->WidgetTree->GetAllWidgets(AllWidgets);
        for (UWidget* W : AllWidgets)
        {
            if (!W)
                continue;
            FBPWidgetInfo Info;
            Info.WidgetName = W->GetName();
            Info.WidgetClass = W->GetClass()->GetPathName();
            if (W->Slot && W->Slot->Parent)
            {
                Info.ParentName = W->Slot->Parent->GetName();
            }
            Out.Add(Info);
        }
        return true;
    };
    if (IsInGameThread()) { Work(); return Out; }
    Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
    return Out;
}

// ============================================================
//  材质表达式枚举（v3.0 · T-20260918-V3-MAT）
// ============================================================

TArray<UMaterialExpression*> UBlueprintPythonBridge::GetMaterialExpressions(UMaterial* Material)
{
    TArray<UMaterialExpression*> Out;
    if (!Material)
        return Out;
    auto Work = [Material, &Out]() -> bool {
        for (const TObjectPtr<UMaterialExpression>& E : Material->GetExpressions())
        {
            if (E)
                Out.Add(E);
        }
        return true;
    };
    if (IsInGameThread())
    {
        Work();
        return Out;
    }
    Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
    return Out;
}

// ============================================================
//  控件属性回读（v3.0.1 · T-20260918-V301-READBACK）
// ============================================================

bool UBlueprintPythonBridge::ReadWidgetProperty(
    UBlueprint* Blueprint, const FString& WidgetName,
    const FString& PropertyName, FString& OutValue)
{
    OutValue.Reset();
    if (!Blueprint)
    {
        OutValue = TEXT("ERROR: Blueprint is null");
        return true;
    }
    auto Work = [Blueprint, WidgetName, PropertyName, &OutValue]() -> bool {
        UWidgetBlueprint* WBP = Cast<UWidgetBlueprint>(Blueprint);
        if (!WBP || !WBP->WidgetTree)
        {
            OutValue = TEXT("ERROR: Not a WidgetBlueprint or WidgetTree missing");
            return true;
        }
        UWidget* W = WBP->WidgetTree->FindWidget(FName(*WidgetName));
        if (!W)
        {
            OutValue = FString::Printf(TEXT("ERROR: Widget '%s' not found"), *WidgetName);
            return true;
        }
        FProperty* Prop = FindFProperty<FProperty>(W->GetClass(), FName(*PropertyName));
        if (!Prop)
        {
            OutValue = FString::Printf(TEXT("ERROR: Property '%s' not found"), *PropertyName);
            return true;
        }
        void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(W);
        Prop->ExportTextItem_Direct(OutValue, ValuePtr, nullptr, W, PPF_None);
        return true;
    };
    if (IsInGameThread())
    {
        Work();
        return true;
    }
    Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
    return true;
}

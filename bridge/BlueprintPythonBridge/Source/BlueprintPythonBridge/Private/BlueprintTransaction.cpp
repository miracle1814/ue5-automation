// =============================================================================
//  BlueprintTransaction.cpp — 任务级撤销（agent transaction）
//  T-20261027-UE5BRIDGE-TASK-UNDO · v4.5
// =============================================================================
//  问题：agent 通过 Python 改图时，很少有人在事务里干活 —— Python 层的
//  "回滚"只能删刚建的节点，撤不回属性修改；编辑器里 Ctrl+Z 也看不到 agent
//  的动作。用户要的是"这整个任务不要了，撤回去"。
//
//  做法（最小侵入，不引入独立撤销栈）：
//    · 每个写命令在 UE 自己的事务栈里留下一步，标题统一 "[agent] <描述>"；
//    · 回滚 = 循环 GEditor->Trans->Undo()，每步先看下一撤销步的标题，
//      不是 "[agent] " 开头就立刻停 —— 用户穿插的操作一步都不碰；
//    · 嵌套 Begin 只在最外层真正开事务（editor_batch / apply_graph 内部调用
//      不会碎成一堆撤销步）；
//    · 已有用户事务进行中 → Begin 返回 false，宿主侧原样执行（不插队）。
//
//  API 版本核对（5.1 与 5.8 头文件逐行比对过，完全一致）：
//    GEditor->BeginTransaction(const FText&) / EndTransaction()
//    GEditor->Trans->IsActive() / Cancel(int32) / Undo(bool) / GetUndoCount()
//    GEditor->Trans->GetUndoContext(bool) -> FTransactionContext（Title 字段）
//  ⚠️ 事务标题由 BeginTransaction 的 Description 原样保存；前缀即安全边界。
// =============================================================================

#include "BlueprintPythonBridge.h"

#include "Editor.h"
#include "Editor/Transactor.h"
#include "Misc/ITransaction.h"
#include "Async/Async.h"

// ---------- GT helper（与 BlueprintPythonBridge.cpp 同口径；文件内自足）----------
namespace
{
    /** 已在 GT → 直呼；否则派发 GameThread 同步等待（fail-loud：不自旋）。 */
    template <typename Func>
    auto AgentTxOnGameThread(Func&& InFunc) -> decltype(InFunc())
    {
        if (IsInGameThread())
        {
            return InFunc();
        }
        return Async(EAsyncExecution::TaskGraphMainThread,
            std::forward<Func>(InFunc)).Get();
    }

    /** 事务标题前缀 —— 回滚安全的唯一判据（改这里必须同步 Python 侧常量）。 */
    const TCHAR* const AgentTxPrefix = TEXT("[agent] ");

    /** 嵌套深度（仅 GameThread 访问，无需原子）。0 = 没有 agent 事务在开。 */
    int32 GAgentTxDepth = 0;

    /** 字符串 → JSON 安全文本（反斜杠 / 引号 / 控制字符）。 */
    FString AgentTxEscapeJson(const FString& In)
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

    FString AgentTxFailJson(const FString& Message)
    {
        return FString::Printf(TEXT("{\"ok\":false,\"error\":\"%s\"}"),
            *AgentTxEscapeJson(Message));
    }

    FString AgentTxNormalizeTitle(const FString& Description)
    {
        FString Title = Description.TrimStartAndEnd();
        if (Title.IsEmpty())
        {
            Title = TEXT("agent edit");
        }
        if (!Title.StartsWith(AgentTxPrefix))
        {
            Title = FString(AgentTxPrefix) + Title;
        }
        return Title;
    }

    /** 可撤销条目数。
     *
     *  ⚠️ UE 语义坑（TransBuffer.h 实证）：`UndoCount` 是**已撤销、可重做**的
     *  条数，不是可撤销数；可撤销数 = QueueLength - UndoCount。
     */
    int32 AgentTxUndoableCount()
    {
        if (!GEditor || !GEditor->Trans)
        {
            return 0;
        }
        return GEditor->Trans->GetQueueLength() - GEditor->Trans->GetUndoCount();
    }

    /** 下一撤销步的标题（空串 = 无可撤销事务）。
     *
     *  ⚠️ 不用 `GetUndoContext()`：5.1 实测事务刚结束时它返回空 context
     *  （bCheckWhetherUndoPossible=false 也不给标题）；队列末位才是下一步撤销。
     */
    FString AgentTxNextUndoTitle()
    {
        if (!GEditor || !GEditor->Trans || AgentTxUndoableCount() <= 0)
        {
            return FString();
        }
        const int32 LastIndex = GEditor->Trans->GetQueueLength() - 1;
        const FTransaction* Tx = GEditor->Trans->GetTransaction(LastIndex);
        return Tx ? Tx->GetTitle().ToString() : FString();
    }

    /** 撤销栈顶连续 agent 事务数（"本任务可一键回滚 N 步"的数据源）。
     *
     *  队列索引语义（UE5.1 SUndoHistory.cpp:355 实证）：
     *    [0, QueueLength-UndoCount) = 可重做（已撤销）；末尾可撤销条数 = 可撤销。
     *  故下一步撤销 = 队列末位，向前数连续带 "[agent] " 标题的事务。
     */
    int32 AgentTxCountTopAgentSteps()
    {
        if (!GEditor || !GEditor->Trans)
        {
            return 0;
        }
        const int32 Undoable = AgentTxUndoableCount();
        if (Undoable <= 0)
        {
            return 0;
        }
        const int32 QueueLength = GEditor->Trans->GetQueueLength();
        int32 AgentSteps = 0;
        for (int32 Index = QueueLength - 1;
             Index >= 0 && AgentSteps < Undoable; --Index)
        {
            const FTransaction* Tx = GEditor->Trans->GetTransaction(Index);
            if (!Tx)
            {
                break;
            }
            if (!Tx->GetTitle().ToString().StartsWith(AgentTxPrefix))
            {
                break;
            }
            ++AgentSteps;
        }
        return AgentSteps;
    }
} // namespace

// -----------------------------------------------------------------------------
//  开启 / 结束 / 取消
// -----------------------------------------------------------------------------

bool UBlueprintPythonBridge::BeginAgentTransaction(const FString& Description)
{
    return AgentTxOnGameThread([Description]() -> bool
    {
        if (!GEditor || !GEditor->Trans)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[BPBridge] BeginAgentTransaction FAILED: GEditor/Trans unavailable"));
            return false;
        }
        // 嵌套：最外层已经开了真实事务，这里只记深度（不碎撤销步）
        if (GAgentTxDepth > 0)
        {
            ++GAgentTxDepth;
            return true;
        }
        // 用户/引擎事务正在进行 → 不插队（End 会替别人关事务，破坏其撤销记录）
        if (GEditor->Trans->IsActive())
        {
            UE_LOG(LogTemp, Log,
                TEXT("[BPBridge] BeginAgentTransaction skipped: another transaction is active"));
            return false;
        }

        const FString Title = AgentTxNormalizeTitle(Description);
        const int32 TxIndex = GEditor->BeginTransaction(FText::FromString(Title));
        GAgentTxDepth = 1;
        UE_LOG(LogTemp, Log,
            TEXT("[BPBridge] AgentTx BEGIN '%s' tx_index=%d active=%d queue=%d undoable=%d redo=%d"),
            *Title, TxIndex,
            (GEditor->Trans->IsActive() ? 1 : 0),
            GEditor->Trans->GetQueueLength(),
            AgentTxUndoableCount(),
            GEditor->Trans->GetUndoCount());
        return true;
    });
}

bool UBlueprintPythonBridge::EndAgentTransaction()
{
    return AgentTxOnGameThread([]() -> bool
    {
        if (!GEditor || !GEditor->Trans)
        {
            return false;
        }
        if (GAgentTxDepth <= 0)
        {
            // 未开启（Begin 因用户事务在场而跳过）→ 绝不能 End 别人的事务
            return false;
        }
        --GAgentTxDepth;
        if (GAgentTxDepth == 0)
        {
            const int32 TxIndex = GEditor->EndTransaction();
            UE_LOG(LogTemp, Log,
                TEXT("[BPBridge] AgentTx END tx_index=%d active=%d queue=%d undoable=%d redo=%d"),
                TxIndex,
                (GEditor->Trans->IsActive() ? 1 : 0),
                GEditor->Trans->GetQueueLength(),
                AgentTxUndoableCount(),
                GEditor->Trans->GetUndoCount());
        }
        return true;
    });
}

bool UBlueprintPythonBridge::CancelAgentTransaction()
{
    return AgentTxOnGameThread([]() -> bool
    {
        if (!GEditor || !GEditor->Trans)
        {
            return false;
        }
        if (GAgentTxDepth <= 0)
        {
            return false;
        }
        GAgentTxDepth = 0;
        // Cancel：丢弃当前事务记录（不产生撤销步）。调用方须已把图复原到
        // 事务前状态（apply_graph 的手工回滚先跑，再 Cancel）。
        if (GEditor->Trans->IsActive())
        {
            GEditor->Trans->Cancel();
        }
        return true;
    });
}

// -----------------------------------------------------------------------------
//  查询 / 回滚
// -----------------------------------------------------------------------------

FString UBlueprintPythonBridge::GetAgentUndoStatus()
{
    return AgentTxOnGameThread([]() -> FString
    {
        if (!GEditor || !GEditor->Trans)
        {
            return AgentTxFailJson(TEXT("GEditor/Trans 不可用（编辑器未就绪）"));
        }
        const int32 Undoable = AgentTxUndoableCount();
        const int32 Redoable = GEditor->Trans->GetUndoCount();
        const FString NextTitle = AgentTxNextUndoTitle();
        const bool bNextIsAgent = !NextTitle.IsEmpty()
            && NextTitle.StartsWith(AgentTxPrefix);

        return FString::Printf(
            TEXT("{\"ok\":true,\"active\":%s,\"depth\":%d,\"undo_count\":%d,")
            TEXT("\"redo_count\":%d,\"agent_undoable\":%d,")
            TEXT("\"next_title\":\"%s\",\"next_is_agent\":%s}"),
            (GEditor->Trans->IsActive() ? TEXT("true") : TEXT("false")),
            GAgentTxDepth,
            Undoable,
            Redoable,
            AgentTxCountTopAgentSteps(),
            *AgentTxEscapeJson(NextTitle),
            (bNextIsAgent ? TEXT("true") : TEXT("false")));
    });
}

FString UBlueprintPythonBridge::UndoAgentTransactions(int32 MaxCount)
{
    return AgentTxOnGameThread([MaxCount]() -> FString
    {
        if (!GEditor || !GEditor->Trans)
        {
            return AgentTxFailJson(TEXT("GEditor/Trans 不可用（编辑器未就绪）"));
        }
        if (GEditor->Trans->IsActive() || GAgentTxDepth > 0)
        {
            return AgentTxFailJson(
                TEXT("事务正在进行中（agent 或用户）—— 先结束/取消当前事务再回滚"));
        }

        const int32 Limit = MaxCount > 0 ? MaxCount : 1;
        const int32 Before = AgentTxUndoableCount();
        int32 Undone = 0;
        FString TitlesJson;
        FString StoppedReason;

        while (Undone < Limit)
        {
            if (AgentTxUndoableCount() <= 0)
            {
                StoppedReason = (Undone == 0)
                    ? TEXT("没有可撤销的事务")
                    : TEXT("撤销栈已空");
                break;
            }
            const FString Title = AgentTxNextUndoTitle();
            if (Title.IsEmpty())
            {
                StoppedReason = (Undone == 0)
                    ? TEXT("没有可撤销的事务")
                    : TEXT("撤销栈已空");
                break;
            }
            if (!Title.StartsWith(AgentTxPrefix))
            {
                StoppedReason = FString::Printf(
                    TEXT("下一个撤销步不是 agent 事务（\"%s\"）—— 用户操作穿插，已停止；")
                    TEXT("不会跳过用户操作去撤更早的 agent 步骤"), *Title);
                break;
            }
            if (!GEditor->Trans->Undo(true))
            {
                StoppedReason = TEXT("引擎拒绝撤销（Undo 返回 false）");
                break;
            }
            if (Undone > 0)
            {
                TitlesJson += TEXT(",");
            }
            TitlesJson += FString::Printf(TEXT("\"%s\""), *AgentTxEscapeJson(Title));
            ++Undone;
        }

        const int32 After = AgentTxUndoableCount();
        return FString::Printf(
            TEXT("{\"ok\":true,\"undone\":%d,\"titles\":[%s],")
            TEXT("\"stopped_reason\":\"%s\",\"undo_count_before\":%d,")
            TEXT("\"undo_count_after\":%d}"),
            Undone, *TitlesJson, *AgentTxEscapeJson(StoppedReason), Before, After);
    });
}

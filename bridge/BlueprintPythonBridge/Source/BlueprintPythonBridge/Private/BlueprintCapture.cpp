// =====================================================================
// ✅ TODO(home) 排错闭环记录（2026-10-24 编译 + 真机实测均已完成）
// =====================================================================
// 编写环境：本机无 UE 引擎 / 无 VS，作者按 BlueprintPythonBridge.cpp 既有
// 模式 + UEBox MIT 参考实现（UnrealAgentLink，版权声明见文件尾）离线撰写。
// 回家实测环境：UE 5.1（VS2019 14.29 工具链）/ UE 5.8.2
// -56702186（VS2022 MSVC 14.44）；真机验证项目为本地 5.8 测试工程。
//
// 5 处 API 形态级风险点的裁决（+ 编译/真机暴露的草稿硬伤）：
//   ① FReversedZPerspectiveMatrix(HalfFov, W, H, MinZ)：4 参重载存在
//      （Core/Public/Math/PerspectiveMatrix.h:69，非 SceneView.h），HalfFOV=
//      水平半角已证实 → 风险解除。⚠️ 但真机实测揪出更深一层：UE 矩阵是
//      行向量约定（P·M），且该投影矩阵按"渲染视空间 Z=深度"设计 ——
//      FInverseRotationMatrix 的局部空间（X=前/Y=右/Z=上）必须先经轴重映射
//      （见 BridgeBuildViewProjection 内 LocalToRenderView，真值对照 NDC
//      误差 <1e-3；第一版把映射写成转置也曾翻车）。
//   ② FScreenshotRequest：签名无 Size 参数，第 3 参是 bAddFilenameSuffix
//      （草稿原传 FIntPoint 已改 false）。真机裁决：Filename 绝对路径【不会】
//      被引擎重定向，按原路径落盘 → 函数内 TODO 解除；仅 PIE 预热期首张
//      落盘可超 2s（宿主侧 capture_pie 轮询已放宽 8s）。
//   ③ GCurrentLevelEditingViewportClient：声明在 Editor.h（两引擎一致），
//      5.1 链接通过 → 风险解除（LevelEditorViewportClient.h 并不存在）。
//   ④ UnrealClient.h include：路径无漂移 → 风险解除。
//   ⑤ TActorIterator：API 可用，补 EngineUtils.h include（草稿漏）；真机
//      73-actor 关卡枚举即时返回，性能无虞。另修：视锥外 actor 的 Screen
//      是栈残留值（顶着上一轮结果）→ 置零回传（坐标诚实性）。
//   草稿硬伤：Engine/WorldContext.h 不存在（FWorldContext 内联在 Engine.h）；
//   辅助函数区漏 #endif；漏 Async/Async.h；include 漂移见行内注
//   （PlayerCameraManager→Camera/、GameViewportClient→Engine/、
//   IImageWrapper 去 Interfaces/ 前缀、LevelEditorViewportClient.h→
//   LevelEditorViewport.h）。
//   ⚠️ 环境坑：仓库路径含中文时 MSVC C1083（PCH 路径乱码）——必须纯 ASCII
//   路径构建。真机验证全记录见 skill/ue5-automation/docs/pitfalls.md B1~B12。
//
// Python 侧契约（改了这里必须同步 ue5_bridge.py）：
//   - 三个入口【恒返回 FString JSON】，失败也不抛异常给 Python：
//       成功 {"ok":true, ...}；失败 {"ok":false,"error":"..."}
//   - 白名单命令 capture_viewport / capture_pie / project_actors_to_screen
//     转发到本文件三个 UFUNCTION（ue5_bridge.py::_COMMAND_WHITELIST）。
// =====================================================================
//
// BlueprintCapture.cpp — 截图反馈闭环（T-20261024-UE5BRIDGE-SCREENSHOT · v3.7）
// =====================================================================
// 目标（P0 规格书《P0-截图反馈闭环设计.md》）：给 agent 装上"眼睛"——
//   修改 → 截图 → 视觉自检 → 修正 的闭环。三个入口：
//
//   ① CaptureViewport(OutPath, CameraMode, Pitch, Yaw, Roll, W, H)
//        camera_mode = "active_viewport"：按当前透视视口的机位拍（编辑器世界
//            用 SceneCapture 同步出图；PIE 运行中自动转 FScreenshotRequest 拍
//            游戏画面——用户看的是游戏世界，截编辑器世界等于答非所问）。
//        camera_mode = "agent_camera"：按给定 Pitch/Yaw/Roll 在视口位置摆一台
//            确定性机位（SceneCapture 路线）——agent 可同机位环绕三连拍，
//            blender-mcp 的 multi-angle 模式。
//   ② CapturePIE(OutPath, W, H)：PIE 运行中截游戏画面（FScreenshotRequest）。
//   ③ ProjectActorsToScreen(ActorNames, CameraMode, Pitch, Yaw, Roll)：
//        把 actor 世界坐标投影到与截图【同一机位】的屏幕空间——宿主侧标注
//        （Pillow 画名字标签）的数据源。视锥外 visible=false。
//
// 实现路线大量移植 UEBox（MIT License，UnrealAgentLink 插件，文件尾附声明），
// 移植中采纳的 5 条已核实经验：
//   1. camera_source 溯源必须回传（viewport/player/agent_camera/fallback，
//      fallback 时调用方拿到的图不可据此下结论）；
//   2. PIE 截图走 FScreenshotRequest 是【延迟一帧】执行的——在 GameThread 上
//      同步轮询文件 = 阻塞帧推进 = 永远等不到（死锁）。故 PIE 路线 C++ 侧
//      【只发起、立即返回 pending JSON】，文件轮询由宿主侧（HTTP 线程）
//      承担（存在 + 大小稳定，2s 超时 fail-loud）。UEBox 用编辑器定时器异步
//      回调解决同一问题；我们选宿主轮询，因为 bridge 的 API 是同步返回形态。
//   3. 最小化窗口白图规避：FScreenshotRequest 路线截图前检查主窗口 IsMinimized
//      → 先 Restore()（进程对自己窗口的操作，最小化下确实能恢复渲染）；
//      SceneCapture 路线不受窗口状态影响，无需检测。
//   4. 图片直接进上下文（宿主侧 base64 image block），不走 URL —— 本文件只管
//      落盘，上下文编码在宿主侧。
//   5. 压缩阶梯（768 长边 + JPEG q80）在宿主侧，本文件出全尺寸 PNG 原图。
// =====================================================================
// 移植自 UEBox plugin "UnrealAgentLink"（MIT License）的 editor.screenshot
// 实现（Source/UnrealAgentLink/Private/Commands/UAL_EditorCommands.cpp，
// Handle_TakeScreenshot / UAL_SetupSceneCapture / UAL_FinishSceneCapture 等）。
//   MIT License Copyright (c) UEBox / UnrealAgentLink authors —— 按 MIT 条款
//   移植，本文件变更部分版权归本仓库所有。
// =====================================================================

// Workaround for VS2022 14.42+ with UE5.1 engine headers（同 BlueprintPythonBridge.cpp 首部）
#ifndef __has_feature
#define __has_feature(x) 0
#endif
#pragma warning(push)
#pragma warning(disable: 4668)
#pragma warning(disable: 4067)

#include "BlueprintPythonBridge.h"

// ---- 截图闭环依赖（全部为引擎常驻模块，Build.cs 无需新增）----
#include "Engine/World.h"
// FWorldContext：本机 5.1/5.8 引擎源码树均无独立 WorldContext.h——它完整内联在
// Engine/Engine.h（5.1 第331行 / 5.8 第351行 struct FWorldContext），由 UnrealEd PCH 常带，
// 显式列出防漂移（原草稿写的 Engine/WorldContext.h 不存在，是 TODO(home) 之外的第 6 处 API 漂移）。
#include "Engine/Engine.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Components/SceneCaptureComponent2D.h"
#include "Camera/CameraComponent.h"
#include "GameFramework/PlayerController.h"
// PlayerCameraManager：本机 5.1/5.8 在 Classes/Camera/（非 GameFramework/）
#include "Camera/PlayerCameraManager.h"
// GameViewportClient：本机 5.1/5.8 在 Classes/Engine/（非 GameFramework/）
#include "Engine/GameViewportClient.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/DateTime.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
// Async(EAsyncExecution::TaskGraphMainThread, ...)：草稿漏 include（5.1/5.8 均在 Core/Public/Async/Async.h）
#include "Async/Async.h"
#include "Modules/ModuleManager.h"
// ImageWrapper：本机 5.1/5.8 头文件直接在 Runtime/ImageWrapper/Public/ 根下（无 Interfaces/ 子目录）
#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
// FScreenshotRequest：截图请求（延迟一帧执行的官方机制）
// TODO(home): 头文件路径 5.1 与 5.8 一致（Runtime/Engine/Public/UnrealClient.h），
//             若报找不到改试 "UnrealClient.h" 不带前缀。
#include "UnrealClient.h"
// 手搓 ViewProjection 所需的矩阵（Core 模块，随 CoreMinimal 常在；显式列出防漂移）
#include "Math/PerspectiveMatrix.h"
#include "Math/RotationMatrix.h"
#include "Math/TranslationMatrix.h"
// TActorIterator（EngineUtils.h）：草稿漏 include，本机 5.1/5.8 均在 Runtime/Engine/Public/EngineUtils.h
#include "EngineUtils.h"

#if WITH_EDITOR
#include "Editor.h"
#include "EditorViewportClient.h"
// FLevelEditorViewportClient + GCurrentLevelEditingViewportClient：本机 5.1/5.8 均无
// LevelEditorViewportClient.h——类在 LevelEditorViewport.h，全局符号在 Editor.h（上方已含）
#include "LevelEditorViewport.h"
#include "Slate/SceneViewport.h"
#include "Framework/Application/SlateApplication.h"
#include "Widgets/SWindow.h"
#include "Engine/GameViewportClient.h"
#endif

DEFINE_LOG_CATEGORY_STATIC(LogBPBridgeCapture, Log, All);

// =====================================================================
// 本地工具：极简 JSON 组装（手拼 + 转义；避免为十来个字段引入 Json 模块依赖）
// =====================================================================

namespace
{
	/** 字符串 → JSON 安全文本（反斜杠/引号/控制字符）。Windows 路径的 '\' 必须转义。 */
	FString BridgeEscapeJson(const FString& In)
	{
		FString Out;
		Out.Reserve(In.Len() + 8);
		for (const TCHAR C : In)
		{
			switch (C)
			{
			case TEXT('\\'): Out += TEXT("\\\\"); break;
			case TEXT('"'):  Out += TEXT("\\\""); break;
			case TEXT('\n'): Out += TEXT("\\n");  break;
			case TEXT('\r'): Out += TEXT("\\r");  break;
			case TEXT('\t'): Out += TEXT("\\t");  break;
			default:
				if (C < 0x20)
				{
					Out += FString::Printf(TEXT("\\u%04x"), (int32)C);
				}
				else
				{
					Out += C;
				}
			}
		}
		return Out;
	}

	/** 组装失败 JSON（统一出口：ok=false + error，绝不向 Python 抛异常）。 */
	FString BridgeCaptureError(const FString& Message)
	{
		UE_LOG(LogBPBridgeCapture, Warning, TEXT("[BPBridge] capture 失败: %s"), *Message);
		return FString::Printf(TEXT("{\"ok\":false,\"error\":\"%s\"}"), *BridgeEscapeJson(Message));
	}

	// -----------------------------------------------------------------
	// 落盘目录与路径消毒（防逃逸 —— 规格书安全约束，权威实现在 C++ 侧）
	// -----------------------------------------------------------------

	/** 约定落盘目录：<项目>/Saved/Screenshots/ue5-automation/（仪表盘画面面板同源） */
	FString BridgeScreenshotDir()
	{
		return FPaths::ConvertRelativePathToFull(
			FPaths::Combine(FPaths::ProjectSavedDir(), TEXT("Screenshots"), TEXT("ue5-automation")));
	}

	/**
	 * 把调用方请求的输出路径消毒到约定目录内（防路径逃逸）。
	 *
	 * 规则：
	 *   - 空 / 纯文件名 / 相对路径 → 落约定目录（含子目录的相对路径也允许）；
	 *   - 绝对路径 → 必须落在约定目录前缀之下（大小写不敏感），否则拒绝；
	 *   - ".." 逐级消毒后仍越界 → 拒绝（fail-loud，绝不静默改写到别处）。
	 *
	 * @return true = OutFullPath 可用；false = OutError 已填
	 */
	bool BridgeNormalizeScreenshotPath(const FString& Requested, FString& OutFullPath, FString& OutError)
	{
		const FString BaseDir = BridgeScreenshotDir();
		// 目录不存在就建（-p 语义，已存在无害）
		if (!IFileManager::Get().MakeDirectory(*BaseDir, /*Tree=*/true))
		{
			OutError = FString::Printf(TEXT("无法创建截图目录: %s"), *BaseDir);
			return false;
		}

		FString Candidate = Requested;
		Candidate.TrimStartAndEndInline();
		if (Candidate.IsEmpty())
		{
			// 未指定 → 时间戳文件名（精确到百分之一秒 + 存在性检查，同秒连拍不覆盖）
			const FString Stamp = FDateTime::Now().ToString(TEXT("%Y%m%d_%H%M%S_%s"));
			Candidate = FString::Printf(TEXT("shot_%s.png"), *Stamp);
			while (IFileManager::Get().FileExists(*(BaseDir / Candidate)))
			{
				Candidate = FString::Printf(TEXT("shot_%s_%d.png"), *Stamp, FMath::RandRange(2, 9999));
			}
		}

		// 统一路径分隔符后再判绝对性（FPaths::IsRelative 对 "C:\..." 与 "/..." 均判绝对）
		FPaths::NormalizeFilename(Candidate);

		// 相对（含纯文件名）→ 落在约定目录内；绝对 → 原样（交给下方逃逸检查裁决）
		FString Full;
		if (FPaths::IsRelative(Candidate))
		{
			Full = BaseDir / Candidate;
		}
		else
		{
			Full = FPaths::ConvertRelativePathToFull(Candidate);
		}

		FPaths::NormalizeFilename(Full);

		// 逃逸检查：完整路径必须位于 BaseDir 之下（大小写不敏感 + 分隔符已归一）
		FString BaseNorm = BaseDir;
		FPaths::NormalizeFilename(BaseNorm);
		if (!Full.StartsWith(BaseNorm, ESearchCase::IgnoreCase))
		{
			OutError = FString::Printf(
				TEXT("输出路径越界（防逃逸拒绝）: %s —— 截图只允许落在 %s 之下"),
				*Full, *BaseNorm);
			return false;
		}
		// ".." 已经被 NormalizeFilename 解析；上面前缀检查是最终裁决
		if (!Full.EndsWith(TEXT(".png")))
		{
			// 统一扩展名（后续按 PNG 编码）
			Full += TEXT(".png");
		}
		OutFullPath = Full;
		return true;
	}

	// -----------------------------------------------------------------
	// 世界与机位
	// -----------------------------------------------------------------

#if WITH_EDITOR
	/**
	 * 正在跑的 PIE 世界；没在跑返回 nullptr。
	 * 多人 PIE 优先挑有 GameViewport 的世界（移植 UEBox UAL_GetPlayWorld 的
	 * 实测经验：Play As Client 的独立服务器排在第一个且没有窗口，拿它截图
	 * 拍到的是谁也没在看的画面）。
	 */
	UWorld* BridgeGetPIEWorld()
	{
		if (!GEngine)
		{
			return nullptr;
		}
		UWorld* FirstPieWorld = nullptr;
		for (const FWorldContext& Context : GEngine->GetWorldContexts())
		{
			if (Context.WorldType == EWorldType::PIE && Context.World())
			{
				if (Context.GameViewport)
				{
					return Context.World();
				}
				if (!FirstPieWorld)
				{
					FirstPieWorld = Context.World();
				}
			}
		}
		return FirstPieWorld;
	}

	/** 当前透视编辑器视口（激活优先，其次第一个透视）。找不到返回 nullptr。 */
	FLevelEditorViewportClient* BridgeGetPerspectiveViewportClient()
	{
		if (!GEditor)
		{
			return nullptr;
		}
		// GCurrentLevelEditingViewportClient：用户最后点击的视口（"当前激活"语义）。
		// TODO(home): 该全局符号在 LevelEditorViewportClient.h（UnrealEd 模块），
		//             5.1/5.8 均存在；若 LNK 报未解析符号，改用 GEditor->GetLevelViewportClients()
		//             首个透视项的兜底分支（下方 else 已有）。
		if (GCurrentLevelEditingViewportClient && GCurrentLevelEditingViewportClient->IsPerspective())
		{
			return GCurrentLevelEditingViewportClient;
		}
		for (FLevelEditorViewportClient* Client : GEditor->GetLevelViewportClients())
		{
			if (Client && Client->IsPerspective())
			{
				return Client;
			}
		}
		return nullptr;
	}

	/**
	 * 机位推导（一次推导，截图与投影共用 —— 两者的坐标必须严格同源）。
	 *
	 *   - active_viewport：编辑器世界 → 当前透视视口的 Location/Rotation/FOV；
	 *                      PIE 世界 → 玩家控制器视图（拿不到回落视口）。
	 *   - agent_camera：   位置 = 当前透视视口位置（无视口 → 世界原点上方 5m），
	 *                      旋转 = 调用方给定 Pitch/Yaw/Roll（确定性机位）。
	 *
	 * OutSource ∈ "viewport" | "player" | "agent_camera" | "fallback"。
	 * fallback = 没找到任何视口、用了兜底机位 —— 调用方【不可】据此图下结论
	 * （UEBox 实测教训：构图随机的图曾被当成场景真实样子白走一轮修复）。
	 */
	void BridgeResolveCameraPose(const FString& CameraMode, float Pitch, float Yaw, float Roll,
		UWorld* InWorld, FVector& OutLocation, FRotator& OutRotation, float& OutFOV, const TCHAR*& OutSource)
	{
		constexpr float DefaultFOV = 90.f;
		OutLocation = FVector(0.f, 0.f, 500.f);
		OutRotation = FRotator(-30.f, 0.f, 0.f);
		OutFOV = DefaultFOV;
		OutSource = TEXT("fallback");

		FLevelEditorViewportClient* Client = BridgeGetPerspectiveViewportClient();
		if (Client)
		{
			OutLocation = Client->GetViewLocation();
			OutRotation = Client->GetViewRotation();
			OutFOV = Client->ViewFOV;
			OutSource = TEXT("viewport");
		}

		// PIE 世界优先玩家视角（UEBox：玩家相机 FOV 常与编辑器视口不同，构图要对上必须带上）
		const bool bPIEWorld = InWorld && InWorld->WorldType == EWorldType::PIE;
		if (bPIEWorld)
		{
			if (APlayerController* PC = InWorld->GetFirstPlayerController())
			{
				FVector Loc; FRotator Rot;
				PC->GetPlayerViewPoint(Loc, Rot);
				OutLocation = Loc;
				OutRotation = Rot;
				if (PC->PlayerCameraManager)
				{
					OutFOV = PC->PlayerCameraManager->GetFOVAngle();
				}
				OutSource = TEXT("player");
			}
		}

		if (CameraMode == TEXT("agent_camera"))
		{
			// 确定性机位：位置取视口（可复现的环绕基准），旋转完全由调用方给定。
			// UE Rotator 构造口径 = (Pitch, Yaw, Roll)（与 FRotator 字段序一致）。
			OutRotation = FRotator(Pitch, Yaw, Roll);
			OutSource = TEXT("agent_camera");
		}
	}

	// -----------------------------------------------------------------
	// SceneCapture 同步出图（编辑器世界 / agent_camera 路线）
	// -----------------------------------------------------------------

	/**
	 * SceneCapture2D → RenderTarget → ReadPixels → PNG 落盘（同步，一次调用完成）。
	 *
	 * 移植 UEBox UAL_SetupSceneCapture + UAL_FinishSceneCapture（同步版）。要点：
	 *   - RT 用 RTF_RGBA8 + TargetGamma=显示伽马：读回字节即视口屏幕值
	 *     （UEBox 逐格式实测：RGBA8+伽马2.2 与视口一致，RGBA8_SRGB 偏暗）；
	 *   - SCS_FinalColor LDR（已过完整后处理与色调映射，最接近所见）；
	 *   - alpha 统一压 255（SceneCapture 出的 alpha 常为 0，直接编码得到全透明
	 *     PNG —— 看起来成功打开却是空白）；
	 *   - 连调两次 CaptureScene（同步路线没有跨帧预热可做；第一帧建立眼适应
	 *     历史，第二帧才是稳定画面 —— 引擎在 GameThread 同帧内识别为重复捕获，
	 *     效果有限，这是 UEBox 同步版同样接受的代价）；
	 *   - 视口锁定固定曝光时镜像之（UEBox 实测：SceneCapture 默认自动曝光，
	 *     与视口固定曝光差 20% 亮度，图不可比）。
	 */
	bool BridgeSceneCaptureToFile(UWorld* World, const FVector& ViewLocation, const FRotator& ViewRotation,
		float ViewFOV, int32 Width, int32 Height, const FString& FullPath,
		FString& OutError, FString& OutMethod)
	{
		if (!GEngine)
		{
			OutError = TEXT("GEngine 不可用");
			return false;
		}
		if (!World)
		{
			OutError = TEXT("拿不到世界（没有打开关卡？）");
			return false;
		}

		// ---- RenderTarget ----
		UTextureRenderTarget2D* RenderTarget = NewObject<UTextureRenderTarget2D>(GetTransientPackage());
		if (!RenderTarget)
		{
			OutError = TEXT("创建 RenderTarget 失败");
			return false;
		}
		RenderTarget->RenderTargetFormat = RTF_RGBA8;
		RenderTarget->TargetGamma = GEngine->GetDisplayGamma();
		RenderTarget->ClearColor = FLinearColor::Black;
		RenderTarget->bAutoGenerateMips = false;
		RenderTarget->InitAutoFormat(Width, Height);
		RenderTarget->UpdateResourceImmediate(true);

		// ---- SceneCapture 组件（注册进世界即可，不 SpawnActor —— 不进大纲、无需管 GC 生命周期）----
		USceneCaptureComponent2D* Capture = NewObject<USceneCaptureComponent2D>(World);
		if (!Capture)
		{
			OutError = TEXT("创建 SceneCaptureComponent2D 失败");
			return false;
		}
		Capture->RegisterComponentWithWorld(World);
		Capture->SetWorldLocationAndRotation(ViewLocation, ViewRotation);
		Capture->TextureTarget = RenderTarget;
		Capture->FOVAngle = ViewFOV;
		Capture->CaptureSource = ESceneCaptureSource::SCS_FinalColorLDR;
		Capture->bCaptureEveryFrame = false;
		Capture->bCaptureOnMovement = false;
		Capture->bAlwaysPersistRenderingState = true;

		// 曝光对齐（简化版）：编辑器视口锁定固定曝光时镜像到本捕获。
		// TODO(home)（增强项，暂不移植）：UEBox 的 UAL_MatchViewLighting 还会把
		// GI/反射方法对齐视口（SceneCapture 默认关 Lumen，夜景会死黑）。首版先
		// 不带；真机若遇到"视口正常截图死黑"，从 UEBox UAL_EditorCommands.cpp
		// UAL_MatchViewLighting 整段移植。
		if (World->WorldType == EWorldType::Editor)
		{
			if (FLevelEditorViewportClient* Client = BridgeGetPerspectiveViewportClient())
			{
				if (Client->ExposureSettings.bFixed)
				{
					FPostProcessSettings& PP = Capture->PostProcessSettings;
					PP.bOverride_AutoExposureMethod = true;
					PP.AutoExposureMethod = EAutoExposureMethod::AEM_Manual;
					PP.bOverride_AutoExposureBias = true;
					PP.AutoExposureBias = Client->ExposureSettings.FixedEV100;
					// min==max 才真的锁死自适应（只设 Method 不够，UEBox 实测）
					PP.bOverride_AutoExposureMinBrightness = true;
					PP.AutoExposureMinBrightness = 1.0f;
					PP.bOverride_AutoExposureMaxBrightness = true;
					PP.AutoExposureMaxBrightness = 1.0f;
				}
				else
				{
					// 视口也在自动曝光：把适应速度调到极大，一帧内收敛（UEBox 实测连渲两帧/抄
					// ExposureSettings 均无效，速度拉满才有效）。
					FPostProcessSettings& PP = Capture->PostProcessSettings;
					PP.bOverride_AutoExposureSpeedUp = true;
					PP.AutoExposureSpeedUp = 100.f;
					PP.bOverride_AutoExposureSpeedDown = true;
					PP.AutoExposureSpeedDown = 100.f;
				}
			}
		}

		// ---- 渲染两帧 + 读回 ----
		Capture->CaptureScene();
		Capture->CaptureScene();

		bool bResult = false;
		FTextureRenderTargetResource* Resource = RenderTarget->GameThread_GetRenderTargetResource();
		TArray<FColor> Pixels;
		if (!Resource)
		{
			OutError = TEXT("RenderTarget 资源不可用");
		}
		else
		{
			FReadSurfaceDataFlags ReadFlags(RCM_UNorm, CubeFace_MAX);
			ReadFlags.SetLinearToGamma(false);
			if (!Resource->ReadPixels(Pixels, ReadFlags) || Pixels.Num() == 0)
			{
				OutError = TEXT("从 RenderTarget 读回像素失败");
			}
			else
			{
				// alpha 压不透明（否则全透明 PNG 假成功，UEBox 经验）
				for (FColor& Pixel : Pixels)
				{
					Pixel.A = 255;
				}

				IImageWrapperModule& ImageWrapperModule =
					FModuleManager::LoadModuleChecked<IImageWrapperModule>(TEXT("ImageWrapper"));
				const TSharedPtr<IImageWrapper> PngWrapper = ImageWrapperModule.CreateImageWrapper(EImageFormat::PNG);
				if (!PngWrapper.IsValid() ||
					!PngWrapper->SetRaw(Pixels.GetData(), Pixels.Num() * sizeof(FColor), Width, Height, ERGBFormat::BGRA, 8))
				{
					OutError = TEXT("PNG 编码失败");
				}
				else if (!FFileHelper::SaveArrayToFile(PngWrapper->GetCompressed(100), *FullPath))
				{
					OutError = FString::Printf(TEXT("写文件失败: %s"), *FullPath);
				}
				else
				{
					bResult = true;
					OutMethod = TEXT("scene_capture");
				}
			}
		}

		// ---- 清理（无论成败）----
		Capture->DestroyComponent();
		RenderTarget = nullptr; // RT 挂 TransientPackage，交 GC

		if (bResult)
		{
			UE_LOG(LogBPBridgeCapture, Log, TEXT("[BPBridge] SceneCapture 截图已保存: %s (%dx%d)"), *FullPath, Width, Height);
		}
		return bResult;
	}

	// -----------------------------------------------------------------
	// FScreenshotRequest 路线（PIE / 游戏画面）
	// -----------------------------------------------------------------

	/**
	 * 最小化窗口白图规避 + 发起 FScreenshotRequest。
	 *
	 * ⚠️ 【只发起，不同步等待】—— RequestScreenshot 是延迟一帧执行的：帧由
	 * 编辑器主循环推进，而本函数正运行在 GameThread 上。在 GameThread 上
	 * sleep 轮询文件 = 阻塞帧推进 = 文件永远不出现（自死锁）。因此本函数
	 * 发起后【立即返回】pending JSON，由宿主侧（HTTP 线程）轮询文件
	 * （存在 + 大小稳定，2s 超时 fail-loud —— 纪律在 ue5_bridge.py 侧实现）。
	 */
	FString BridgeRequestScreenshot(const FString& FullPath, int32 Width, int32 Height)
	{
		// ---- 最小化检测（UEBox 经验：PrintWindow/视口截图对最小化窗口回白图且照常成功）----
		// Restore() 是进程对自己窗口的操作，最小化下确实能恢复渲染；
		// 不抢焦点（Windows 不允许后台进程抢焦点，BringToFront 对失焦窗口静默无效 —— UEBox 教训）。
		if (FSlateApplication::IsInitialized())
		{
			TSharedPtr<SWindow> MainWindow = FSlateApplication::Get().GetActiveTopLevelWindow();
			if (!MainWindow.IsValid())
			{
				const TArray<TSharedRef<SWindow>> Windows = FSlateApplication::Get().GetInteractiveTopLevelWindows();
				if (Windows.Num() > 0)
				{
					MainWindow = Windows[0];
				}
			}
			if (MainWindow.IsValid() && MainWindow->GetNativeWindow().IsValid() &&
				MainWindow->GetNativeWindow()->IsMinimized())
			{
				MainWindow->Restore();
				UE_LOG(LogBPBridgeCapture, Log, TEXT("[BPBridge] 主窗口已从最小化还原（白图规避）"));
			}
		}

		// FScreenshotRequest::RequestScreenshot(Filename, bShowUI, bAddFilenameSuffix)：
		//   实际签名（UnrealClient.h，5.1:152 / 5.8 同）只有两个重载，无 Size 参数——
		//   草稿原写的 FIntPoint(Width, Height) 第 3 参实为 bAddFilenameSuffix（编译期已证伪）。
		//   - Filename 带绝对路径 + ".png" 后缀 → 引擎按该路径落盘；
		//   - bShowUI=false：不叠编辑器 UI；bAddFilenameSuffix=false：不加自动序号后缀；
		//   - 尺寸由调用方视口决定（Size 参数本就不存在，超采样走 SceneCapture 路线）。
		// TODO(home): 真机验证 Filename 绝对路径是否被引擎重定向到 Saved/Screenshots/
		//             （若重定向，宿主侧轮询会超时 —— 届时改传纯文件名并让宿主在约定目录下轮询）。
		FScreenshotRequest::RequestScreenshot(FullPath, /*bShowUI=*/false, /*bAddFilenameSuffix=*/false);
		UE_LOG(LogBPBridgeCapture, Log, TEXT("[BPBridge] FScreenshotRequest 已发起（延迟一帧执行，宿主侧轮询）: %s"), *FullPath);
		return FullPath;
	}

	// -----------------------------------------------------------------
	// 世界→屏幕投影（与截图同机位同源；宿主侧标注的数据基础）
	// -----------------------------------------------------------------

	/**
	 * 单点世界坐标 → 屏幕像素（手搓 ViewProjection，无版本敏感 API）。
	 *
	 * @param bClampToFrustum false 时不做视锥判定（visible 由调用方组合）
	 * @return true = 在视锥内（visible 候选）；false = 视锥外
	 *
	 * 矩阵口径（UE 相机 view space 前方为 +X）：
	 *   ViewMatrix      = FInverseRotationMatrix(Rotation) * FTranslationMatrix(-Location)
	 *   ProjectionMatrix = FReversedZPerspectiveMatrix(HalfFovRad, Width, Height, NearZ)
	 *                      —— UE 的 ViewFOV 是【水平】FOV，该重载按宽度定义。
	 * 屏幕映射：X=(NdcX*0.5+0.5)*W；Y=(1-(NdcY*0.5+0.5))*H（NDC y 向上、像素 y 向下）。
	 * TODO(home): FReversedZPerspectiveMatrix(HalfFov, Width, Height, MinZ) 重载语义
	 *             （第 1 参是否半角水平 FOV）在 SceneView.h，5.1/5.8 编译时确认；
	 *             若语义不符，改用 (HalfFovX, HalfFovY, MultX, MultY, MinZ, MaxZ) 重载
	 *             自行传 HalfFovX=HalfFovY=HalfFov, MultX=1, MultY=Width/Height。
	 */
	bool BridgeProjectWorldToScreen(const FMatrix& ViewProjection, int32 ScreenWidth, int32 ScreenHeight,
		const FVector& WorldLocation, FVector2D& OutScreen)
	{
		const FVector4 Clip = ViewProjection.TransformFVector4(FVector4(WorldLocation, 1.f));
		// W ≤ 0 = 相机后方（透视裁剪面之后）
		if (Clip.W <= KINDA_SMALL_NUMBER)
		{
			return false;
		}
		const float NdcX = Clip.X / Clip.W;
		const float NdcY = Clip.Y / Clip.W;
		// 2% 容差：贴边（如正好在屏幕边缘的标注目标）仍算可见，交由调用方按需过滤
		if (NdcX < -1.02f || NdcX > 1.02f || NdcY < -1.02f || NdcY > 1.02f)
		{
			return false;
		}
		OutScreen.X = (NdcX * 0.5f + 0.5f) * ScreenWidth;
		OutScreen.Y = (1.f - (NdcY * 0.5f + 0.5f)) * ScreenHeight;
		return true;
	}

	/** 组装 ViewProjection 矩阵（截图与投影共用同一推导，保证标注坐标与图对齐）。 */
	FMatrix BridgeBuildViewProjection(const FVector& ViewLocation, const FRotator& ViewRotation,
		float ViewFOV, int32 ScreenWidth, int32 ScreenHeight)
	{
		// ⚠️ 真机实测修正（2026-10-24）：FInverseRotationMatrix 的局部视空间是 X=前/Y=右/Z=上，
		// 而 FReversedZPerspectiveMatrix 按 UE 渲染约定"Z=深度"设计。直接相乘会把"高度分量"
		// 灌进 clip.W —— 真机症状：机位正对目标的 actor 也判 visible=false。
		// 与官方 MCP EditorAppToolset.WorldPosToScreenCoords 真值对照（NDC 误差 <1e-3）确认
		// 轴映射：渲染视空间 X=局部Y、Y=局部Z、Z(深度)=局部X。⚠️ UE 矩阵是行向量约定
		// （P·M），行向量下第一版把映射写成了转置（(x,y,z)→(z,x,y)）—— 已按 P·M 语义修正。
		const FMatrix LocalToRenderView = FMatrix(
			FPlane(0.f, 0.f, 1.f, 0.f),   // P'.z += P.x（渲染 Z=深度 ← 局部 X）
			FPlane(1.f, 0.f, 0.f, 0.f),   // P'.x += P.y（渲染 X ← 局部 Y）
			FPlane(0.f, 1.f, 0.f, 0.f),   // P'.y += P.z（渲染 Y ← 局部 Z）
			FPlane(0.f, 0.f, 0.f, 1.f));
		const FMatrix ViewMatrix = FTranslationMatrix(-ViewLocation) * FInverseRotationMatrix(ViewRotation) * LocalToRenderView;
		const float HalfFovRad = FMath::DegreesToRadians(FMath::Clamp(ViewFOV, 1.f, 179.f)) * 0.5f;
		// 近平面用引擎默认（只影响 Z 分量，不影响本工具关心的 X/Y/W）
		const FMatrix ProjMatrix = FReversedZPerspectiveMatrix(HalfFovRad, (float)ScreenWidth, (float)ScreenHeight, GNearClippingPlane);
		return ViewMatrix * ProjMatrix;
	}
#endif // WITH_EDITOR（245 行开：编辑器辅助函数区 —— 草稿漏了此 #endif，编译期证伪后补上）
} // namespace

// =====================================================================
// ① CaptureViewport
// =====================================================================
FString UBlueprintPythonBridge::CaptureViewport(const FString& OutPath, const FString& CameraMode,
	float Pitch, float Yaw, float Roll, int32 Width, int32 Height)
{
#if WITH_EDITOR
	// 参数校验（同本桥既有风格：fail-loud，绝不猜）
	const FString Mode = CameraMode.TrimStartAndEnd().ToLower();
	if (Mode != TEXT("active_viewport") && Mode != TEXT("agent_camera"))
	{
		return BridgeCaptureError(FString::Printf(
			TEXT("camera_mode 非法: %s（可选 active_viewport | agent_camera）"), *CameraMode));
	}
	if (Width < 0 || Height < 0)
	{
		return BridgeCaptureError(TEXT("width/height 不可为负（0 = 自动）"));
	}

	auto Work = [OutPath, Mode, Pitch, Yaw, Roll, Width, Height]() -> FString
	{
		FString FullPath;
		FString PathError;
		if (!BridgeNormalizeScreenshotPath(OutPath, FullPath, PathError))
		{
			return BridgeCaptureError(PathError);
		}

		// PIE 运行中：转游戏画面路线（用户看的是游戏世界）
		if (UWorld* PieWorld = BridgeGetPIEWorld())
		{
			const FString FinalPath = BridgeRequestScreenshot(FullPath, Width, Height);
			return FString::Printf(
				TEXT("{\"ok\":true,\"pending\":true,\"method\":\"screenshot_request\",")
				TEXT("\"saved_path\":\"%s\",\"camera_source\":\"player\",")
				TEXT("\"note\":\"PIE 截图由引擎延迟一帧执行；宿主侧轮询文件存在+大小稳定，2s 超时必须报错\"}"),
				*BridgeEscapeJson(FinalPath));
		}

		// 编辑器世界：机位推导 → SceneCapture 同步出图
		UWorld* EditorWorld = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
		FVector ViewLocation; FRotator ViewRotation; float ViewFOV; const TCHAR* CameraSource = nullptr;
		BridgeResolveCameraPose(Mode, Pitch, Yaw, Roll, EditorWorld, ViewLocation, ViewRotation, ViewFOV, CameraSource);

		// 尺寸：0 = 视口尺寸，无视口则 1280x720（agent 多角度三连拍要固定尺寸才可比）
		int32 W = Width, H = Height;
		if ((W <= 0 || H <= 0))
		{
			if (FLevelEditorViewportClient* Client = BridgeGetPerspectiveViewportClient())
			{
				if (Client->Viewport)
				{
					const FIntPoint Size = Client->Viewport->GetSizeXY();
					W = Size.X; H = Size.Y;
				}
			}
			if (W <= 0 || H <= 0)
			{
				W = 1280; H = 720;
			}
		}

		FString OutError;
		FString Method;
		if (!BridgeSceneCaptureToFile(EditorWorld, ViewLocation, ViewRotation, ViewFOV, W, H, FullPath, OutError, Method))
		{
			return BridgeCaptureError(OutError);
		}

		return FString::Printf(
			TEXT("{\"ok\":true,\"pending\":false,\"method\":\"%s\",\"saved_path\":\"%s\",")
			TEXT("\"width\":%d,\"height\":%d,\"camera_source\":\"%s\",")
			TEXT("\"camera_location\":[%.2f,%.2f,%.2f],\"camera_rotation\":[%.2f,%.2f,%.2f],")
			TEXT("\"fov\":%.1f,\"warn_fallback\":%s}"),
			*Method, *BridgeEscapeJson(FullPath), W, H, CameraSource,
			ViewLocation.X, ViewLocation.Y, ViewLocation.Z,
			ViewRotation.Pitch, ViewRotation.Yaw, ViewRotation.Roll,
			ViewFOV,
			// fallback 机位 = 构图不可信，宿主必须把警告透传给模型（UEBox 经验 1）
			(FString(CameraSource) == TEXT("fallback") ? TEXT("true") : TEXT("false")));
	};
	if (IsInGameThread()) return Work();
	return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
#else
	return BridgeCaptureError(TEXT("CaptureViewport 仅在编辑器构建可用"));
#endif
}

// =====================================================================
// ② CapturePIE
// =====================================================================
FString UBlueprintPythonBridge::CapturePIE(const FString& OutPath, int32 Width, int32 Height)
{
#if WITH_EDITOR
	auto Work = [OutPath, Width, Height]() -> FString
	{
		UWorld* PieWorld = BridgeGetPIEWorld();
		if (!PieWorld)
		{
			return BridgeCaptureError(TEXT("PIE/Simulate 未在运行 —— 先 pie_start 再截图（绝不假成功）"));
		}
		FString FullPath;
		FString PathError;
		if (!BridgeNormalizeScreenshotPath(OutPath, FullPath, PathError))
		{
			return BridgeCaptureError(PathError);
		}
		const FString FinalPath = BridgeRequestScreenshot(FullPath, Width, Height);
		return FString::Printf(
			TEXT("{\"ok\":true,\"pending\":true,\"method\":\"screenshot_request\",")
			TEXT("\"saved_path\":\"%s\",\"camera_source\":\"player\",")
			TEXT("\"world\":\"%s\",")
			TEXT("\"note\":\"PIE 截图延迟一帧执行；宿主侧轮询文件存在+大小稳定，2s 超时必须报错\"}"),
			*BridgeEscapeJson(FinalPath), *BridgeEscapeJson(PieWorld->GetMapName()));
	};
	if (IsInGameThread()) return Work();
	return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
#else
	return BridgeCaptureError(TEXT("CapturePIE 仅在编辑器构建可用"));
#endif
}

// =====================================================================
// ③ ProjectActorsToScreen
// =====================================================================
FString UBlueprintPythonBridge::ProjectActorsToScreen(const TArray<FString>& ActorNames,
	const FString& CameraMode, float Pitch, float Yaw, float Roll)
{
#if WITH_EDITOR
	const FString Mode = CameraMode.TrimStartAndEnd().ToLower();
	if (Mode != TEXT("active_viewport") && Mode != TEXT("agent_camera"))
	{
		return BridgeCaptureError(FString::Printf(
			TEXT("camera_mode 非法: %s（可选 active_viewport | agent_camera）"), *CameraMode));
	}
	if (ActorNames.Num() == 0)
	{
		return BridgeCaptureError(TEXT("actor_names 为空 —— 先 find_actors 拿名字"));
	}

	auto Work = [ActorNames, Mode, Pitch, Yaw, Roll]() -> FString
	{
		// PIE 在跑时投影 PIE 世界（与 CaptureViewport 的世界选择规则一致：
		// 用户看的是游戏世界，标注也必须标在游戏画面上）
		UWorld* World = BridgeGetPIEWorld();
		FString WorldLabel = TEXT("pie");
		if (!World)
		{
			World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
			WorldLabel = TEXT("editor");
		}
		if (!World)
		{
			return BridgeCaptureError(TEXT("拿不到世界（没有打开关卡？）"));
		}

		FVector ViewLocation; FRotator ViewRotation; float ViewFOV; const TCHAR* CameraSource = nullptr;
		BridgeResolveCameraPose(Mode, Pitch, Yaw, Roll, World, ViewLocation, ViewRotation, ViewFOV, CameraSource);

		// 屏幕尺寸：与 BridgeResolveCameraPose 的来源一致（视口 / PIE GameViewport / 默认）
		int32 W = 1280, H = 720;
		if (World->WorldType == EWorldType::PIE && World->GetGameViewport())
		{
			const FIntPoint Size = World->GetGameViewport()->GetGameViewport()->GetSizeXY();
			W = Size.X; H = Size.Y;
		}
		else if (FLevelEditorViewportClient* Client = BridgeGetPerspectiveViewportClient())
		{
			if (Client->Viewport)
			{
				const FIntPoint Size = Client->Viewport->GetSizeXY();
				W = Size.X; H = Size.Y;
			}
		}

		const FMatrix ViewProjection = BridgeBuildViewProjection(ViewLocation, ViewRotation, ViewFOV, W, H);

		// 按 actor 名匹配（限当前世界；GetAllActors 逐名 find 开销可接受 —— 上限由调用方控制）
		TArray<FString> Items;
		int32 VisibleCount = 0;
		for (const FString& Name : ActorNames)
		{
			// TODO(home): 引擎 FindActor 没有按 Name/Label 的公开单参接口 —— 用 TActorIterator
			//             逐个比对 GetName()/GetActorLabel()（编辑器世界标签优先）。
			//             5.1 与 5.8 均可用 EngineUtils.h 的 TActorIterator<AActor>。
			AActor* Found = nullptr;
			for (TActorIterator<AActor> It(World); It; ++It)
			{
				AActor* A = *It;
				if (!A) continue;
				if (A->GetName() == Name) { Found = A; break; }
#if WITH_EDITOR
				// 编辑器世界的 Label（大纲显示名）同样可寻址；PIE 世界 GetActorLabel 不可用
				if (World->WorldType != EWorldType::PIE && A->GetActorLabel() == Name) { Found = A; break; }
#endif
			}

			if (!Found)
			{
				// 找不到的 actor 如实报 visible=false + note（绝不编造坐标）
				Items.Add(FString::Printf(
					TEXT("{\"name\":\"%s\",\"x\":0,\"y\":0,\"visible\":false,\"found\":false}"),
					*BridgeEscapeJson(Name)));
				continue;
			}

			FVector2D Screen;
			const bool bInFrustum = BridgeProjectWorldToScreen(ViewProjection, W, H, Found->GetActorLocation(), Screen);
			if (bInFrustum)
			{
				++VisibleCount;
			}
			// 视锥外不回传坐标（BridgeProjectWorldToScreen 提前 return 时 Screen 未写，
			// 栈值会残留上一轮迭代结果 —— 真机实测：invisible 的 x/y 顶着前一个 actor
			// 的坐标，读的人会误当有效位置）；置 0 与 not-found 口径一致。
			if (!bInFrustum)
			{
				Screen = FVector2D::ZeroVector;
			}
			Items.Add(FString::Printf(
				TEXT("{\"name\":\"%s\",\"x\":%.0f,\"y\":%.0f,\"visible\":%s,\"found\":true,")
				TEXT("\"world_location\":[%.2f,%.2f,%.2f]}"),
				*BridgeEscapeJson(Name), Screen.X, Screen.Y,
				bInFrustum ? TEXT("true") : TEXT("false"),
				Found->GetActorLocation().X, Found->GetActorLocation().Y, Found->GetActorLocation().Z));
		}

		// TODO(home)（Phase 2 增强项，暂不做）：遮挡检测（LineTraceSingleByChannel）——
		// 规格书允许首版 visible 只表达"视锥内"，遮挡留给 Phase 2。

		FString ItemsJoined = FString::Join(Items, TEXT(","));
		return FString::Printf(
			TEXT("{\"ok\":true,\"world\":\"%s\",\"camera_source\":\"%s\",\"screen\":[%d,%d],")
			TEXT("\"camera_location\":[%.2f,%.2f,%.2f],\"camera_rotation\":[%.2f,%.2f,%.2f],")
			TEXT("\"visible_count\":%d,\"projections\":[%s]}"),
			*WorldLabel, CameraSource, W, H,
			ViewLocation.X, ViewLocation.Y, ViewLocation.Z,
			ViewRotation.Pitch, ViewRotation.Yaw, ViewRotation.Roll,
			VisibleCount, *ItemsJoined);
	};
	if (IsInGameThread()) return Work();
	return Async(EAsyncExecution::TaskGraphMainThread, MoveTemp(Work)).Get();
#else
	return BridgeCaptureError(TEXT("ProjectActorsToScreen 仅在编辑器构建可用"));
#endif
}

#pragma warning(pop)

/*
=============================================================================
Portions of this file are derived from UEBox / UnrealAgentLink
(https://github.com/ueboxai/uebox). The upstream plugin README declares MIT;
the repository root declares Apache-2.0. The full MIT text is reproduced here
to travel with this source file; the Apache-2.0 text is in the repository
LICENSE.

MIT License

Copyright (c) UEBox / UnrealAgentLink authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
=============================================================================
*/

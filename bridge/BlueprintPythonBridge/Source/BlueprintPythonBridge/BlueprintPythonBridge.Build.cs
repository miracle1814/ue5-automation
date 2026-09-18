using UnrealBuildTool;

public class BlueprintPythonBridge : ModuleRules
{
    public BlueprintPythonBridge(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

        // UE5.1 + VS2022 14.42: __has_feature is a Clang built-in unknown to MSVC
        PrivateDefinitions.Add("__has_feature(x)=0");
        bEnableUndefinedIdentifierWarnings = false;

        PublicDependencyModuleNames.AddRange(new string[] {
            "Core",
            "CoreUObject",
            "Engine",
            "UnrealEd",
            "BlueprintGraph",
            "KismetCompiler",
            "Kismet",
            // T-20260910-PATCH-B: FKey（InputCoreTypes.h）为 InputCore 模块符号，
            // 编译期经 Engine 传递可用，但链接期需显式依赖，否则 LNK2019（FKey::FKey/IsValid/GetFName/~FKey）
            "InputCore"
        });

        PrivateDependencyModuleNames.AddRange(new string[] {
            "Slate",
            "SlateCore",
            "PythonScriptPlugin",
            // v3.0（T-20260918-V3-UMG）：UMG 控件蓝图支持
            "UMG",
            "UMGEditor"
        });
    }
}

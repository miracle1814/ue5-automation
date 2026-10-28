# Windows 安装与连接

本 Skill 只包含操作规范；运行包（UE 插件 + bridge）单独分发，避免把原生 DLL
塞进 Skill 包。

## 1. 准备

- Windows 10/11 x64
- UE 5.1.x（开箱）或 UE 5.8.x（预编译 DLL）
- 项目已启用：
  - `Python Editor Script Plugin`
  - `Editor Scripting Utilities`
- 关闭其他占用 8889 端口的编辑器实例，或给当前实例设置
  `UE5_BRIDGE_PORT=8890` 后重启。

## 2. 安装运行包

从 GitHub Release 下载与当前 Skill 版本匹配的 portable 包：

```text
https://github.com/miracle1814/ue5-automation/releases/download/v6.5.4/ue5-automation-v6.5.4-portable.zip
```

Release 页面（含 SHA256 与更新说明）：

```text
https://github.com/miracle1814/ue5-automation/releases/tag/v6.5.4
```

解压后：

```bat
install.bat
```

把 `.uproject` 拖进窗口，脚本会：

1. 读 EngineAssociation；
2. 选 5.1 / 5.8 DLL；
3. 安装插件到 `Plugins/BlueprintPythonBridge`；
4. 安装宿主脚本到 `Content/Python`；
5. 写 `init_unreal.py` 自启；
6. 跑环境自检，看到 `FAIL 0` 即成功。

> 如果项目主页暂未提供当前版本的 portable 包，请使用仓库源码里的
> `packaging/make_portable.py` 构建，或让作者提供运行包；不要用旧版本 DLL
> 冒充当前版本。

## 3. 启动编辑器

重启 UE 编辑器，Output Log 搜索 `ue5_bridge`，应看到：

```text
[ue5_bridge] [OK] 服务已在后台启动 → http://127.0.0.1:8889/docs
```

再搜索 `BlueprintPythonBridge`，不应出现
`Incompatible or missing module`。

## 4. 连接 MCP

HTTP：

```text
http://127.0.0.1:8889/mcp
Header: X-Skill-Token: <token>
```

token 文件：

```text
%APPDATA%\ue5_bridge\token
```

stdio 客户端：

```bat
python Content\Python\ue5_mcp_stdio.py
```

一键探测并写入常见客户端配置：

```bat
python Content\Python\install_mcp.py --dry-run
python Content\Python\install_mcp.py
```

## 5. 自检

```json
{"command":"health","params":{}}
```

应返回：

```json
{"status":"ok","ue_info":{"engine_version":"5.x","bridge_loaded":true}}
```

如果 403：token 不匹配；如果连接失败：编辑器未启动、插件未加载、端口被占。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 没有“服务已在后台启动” | 检查插件启用、`Content/Python/init_unreal.py` 是否存在 |
| `Incompatible or missing module` | DLL 与引擎版本不匹配，重跑 install.bat |
| FastAPI/uvicorn 未安装 | 用引擎自带 Python 安装：`python.exe -m pip install fastapi uvicorn pydantic` |
| 403 | 读 `%APPDATA%\ue5_bridge\token`，与客户端配置保持一致 |
| 端口占用 | 关掉另一个编辑器，或设 `UE5_BRIDGE_PORT=8890` 后重启 |

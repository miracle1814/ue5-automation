# Third-Party Notices

This project is licensed under Apache-2.0. The notices below cover third-party
material included in, or referenced by, this distribution.

## UEBox / UnrealAgentLink (MIT / Apache-2.0)

Portions of the screenshot/PIE capture implementation in
`bridge/BlueprintPythonBridge/Source/BlueprintPythonBridge/Private/BlueprintCapture.cpp`
are derived from UEBox / UnrealAgentLink
(https://github.com/ueboxai/uebox).

Upstream status: the `plugin/UnrealAgentLink` README declares **MIT**; the
repository root `LICENSE` declares **Apache-2.0**. This project preserves the
copyright/attribution below, includes the full MIT text below, and also ships
the Apache-2.0 license text in `LICENSE`. If either upstream license applies,
both sets of terms are satisfied for the copied portions.

The dashboard styling also references UEBox's theme/typography token approach at
the design level; no UEBox assets, fonts, or images are bundled.

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

## Unreal Engine / Epic Games

Unreal, Unreal Engine, UE, and the Unreal logo are trademarks or registered
trademarks of Epic Games, Inc. in the United States and elsewhere. This project
is an independent developer tool and is not affiliated with, sponsored by, or
endorsed by Epic Games.

The plugin is built against Unreal Engine headers and binaries. Those engine
components remain subject to the Unreal Engine End User License Agreement and
are not relicensed by this project's Apache-2.0 license. The Apache-2.0 license
covers this project's original source code only.

No Epic Games artwork, logos, sample assets, or marketplace content are bundled.

## Python dependencies

The host scripts import the following packages but do not vendor their source:

| Package | License |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| pydantic | MIT |
| requests | Apache-2.0 |
| PyYAML | MIT |
| pytest / pytest-timeout | MIT |
| httpx | BSD-3-Clause |

Install them from their official distributions; their license texts are
available in their respective packages/repositories.

## External services

The optional report renderer can call `mermaid.ink` for online Mermaid rendering.
That service is third-party and is not part of this distribution; do not send
confidential diagrams to it unless your policy allows it. The dashboard badges
in `README` are served by shields.io and Glama and are not bundled.

## Attribution / provenance note

`skill/ue5-automation/scripts/mermaid_render.py` was authored by the project
owner under an earlier agent handle (`Xiaoxi / mWNCXY`); copyright belongs to
the project owner. The header is now unified to `miracle1814`. No third-party
claim is made for this file.

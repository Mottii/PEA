# Third-Party Notices

PEA (Physics Experiments Automation) is licensed under the [MIT License](./LICENSE).
When bootstrapped locally via [`scripts/fetch_assets.py`](./scripts/fetch_assets.py) and
`pyproject.toml`, PEA downloads or depends on the following third-party open-source
assets and libraries. None of the binary asset payloads are redistributed inside the
git repository or `.mcpb` bundle; they are fetched directly from their upstream sources
with SHA-256 verification.

---

## Pinned Offline Runtime Assets (`scripts/fetch_assets.py`)

| Asset | Pinned Version | License | Upstream Source |
| :--- | :--- | :--- | :--- |
| **KaTeX** (CSS, JS, auto-render, WOFF2 fonts) | `0.16.21` | MIT License | <https://github.com/KaTeX/KaTeX> (`registry.npmjs.org/katex/-/katex-0.16.21.tgz`) |
| **Ruffle** (Flash/SWF WebAssembly runtime) | `0.7.1` | Dual-licensed Apache-2.0 or MIT | <https://github.com/ruffle-rs/ruffle> (`registry.npmjs.org/@ruffle-rs/ruffle/-/ruffle-0.7.1.tgz`) |
| **Noto Sans** (Regular & Bold hinted TTF fonts) | `2.008` (hinted) | SIL Open Font License 1.1 (OFL-1.1) | <https://github.com/notofonts/noto-fonts> |
| **RapidOCR / PaddleOCR PP-OCRv5 Mobile ONNX Models** (`ch_PP-OCRv5_det_mobile.onnx`, `cyrillic_PP-OCRv5_rec_mobile.onnx`) | `v3.10.0` | Apache-2.0 License | <https://github.com/RapidAI/RapidOCR> / <https://github.com/PaddlePaddle/PaddleOCR> |

Each fetched asset group preserves its upstream license file under `local_assets/`
(`local_assets/katex/LICENSE`, `local_assets/ruffle/LICENSE_APACHE`,
`local_assets/ruffle/LICENSE_MIT`, `local_assets/fonts/LICENSE.txt`).

---

## Core Python Runtime Dependencies (`pyproject.toml`)

| Package | License | Project URL |
| :--- | :--- | :--- |
| **mcp** (Model Context Protocol Python SDK) | MIT | <https://github.com/modelcontextprotocol/python-sdk> |
| **pydantic** | MIT | <https://github.com/pydantic/pydantic> |
| **playwright** | Apache-2.0 | <https://github.com/microsoft/playwright-python> |
| **rapidocr** | Apache-2.0 | <https://github.com/RapidAI/RapidOCR> |
| **onnxruntime** | MIT | <https://github.com/microsoft/onnxruntime> |
| **Pillow** | MIT-CMU | <https://github.com/python-pillow/Pillow> |
| **NumPy** | BSD-3-Clause | <https://github.com/numpy/numpy> |
| **SciPy** | BSD-3-Clause | <https://github.com/scipy/scipy> |
| **Matplotlib** | PSF-based (Matplotlib License) | <https://github.com/matplotlib/matplotlib> |
| **schemdraw** | MIT | <https://bitbucket.org/cdelker/schemdraw> |
| **pypdfium2** | Apache-2.0 / BSD-3-Clause (PDFium: BSD-3-Clause / Apache-2.0) | <https://github.com/pypdfium2-team/pypdfium2> |
| **pypdf** | BSD-3-Clause | <https://github.com/py-pdf/pypdf> |
| **python-docx** | MIT | <https://github.com/python-openxml/python-docx> |
| **Starlette** | BSD-3-Clause | <https://github.com/encode/starlette> |
| **Uvicorn** | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| **HTTPX** | BSD-3-Clause | <https://github.com/encode/httpx> |

---

## Note on User-Provided Laboratory Materials

PEA is strictly lab-agnostic. Any third-party university handouts (`.pdf`) or Flash
simulators (`.swf`) that users stage locally inside `local_assets/sims/` or
`local_assets/benchmarks/` remain subject to their respective copyright holders' terms
and are excluded from version control and `.mcpb` distributions via `.gitignore` and
`scripts/build_mcpb.py`.

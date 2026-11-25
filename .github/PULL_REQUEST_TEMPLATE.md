## Summary
Briefly describe the changes in this pull request and why they are needed.

## Type of Change
- [ ] Bug fix
- [ ] New feature (lab-agnostic)
- [ ] Documentation / community polish
- [ ] CI / packaging / tooling

## Verification Checklist
- [ ] `.venv/bin/ruff check src/ tests/ scripts/` passes with 0 errors
- [ ] `.venv/bin/pytest tests/ -m "not live_sim" -v` passes
- [ ] `.venv/bin/pytest tests/e2e/test_transient_m2_e2e.py -m live_sim -v` passes (if simulator/OCR/report/viewer code changed)
- [ ] `.venv/bin/python scripts/build_mcpb.py --check` passes
- [ ] No third-party copyrighted binaries (`.swf`, `.pdf`, `.docx`, `.onnx`, `.wasm`, `.mcpb`), secrets, or literal host home paths committed
- [ ] Provenance separation (`simulator_captured`, `bench_manual`, `model_computed`) and security boundaries preserved

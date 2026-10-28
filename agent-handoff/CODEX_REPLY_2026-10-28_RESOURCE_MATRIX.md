# Codex 回执：Linux 原生资源路由矩阵

- **IMPLEMENTED**：修正无效 APK 文件包装对象在 valid 检查前调用 length 的 probe 缺陷；新增独立矩阵驱动和验收器。
- **RUNTIME_OBSERVED_LIMITED**：真实 ARM64 Linux `libg.so` 的13个合成资源案例符合既有 Android E1 对应路由规则；9个有效文件 native/wrapper exit0，4个缺失 native60/wrapper1。
- **FAILURE_PRESERVED**：首轮12/13，错误调用 length 触发 native95；修复后第二轮13/13；第三轮补全缺失路径 provider release、dlclose 和 asset finish，13/13、独立验收 exit0。
- **UNIT_PASS**：Linux 专项973 passed、1 skipped，exit0。
- **UNRESOLVED**：全引擎生命周期清理、Android/Linux 相同字节配对、异常矩阵、Backup 与完整 catalog 未通过。
- **NOT_RUN**：ResourceManager、GameState、Step、完整对局、no-VM 验收及10×。

基线 `afdeb3333f5ec08fc2e6a71343b918207c9a878b`；交付 SHA 以包含本回执的提交为准。
详见[报告](PURE_LINUX_RESOURCE_MATRIX_2026-10-28.md)和[机器证据](pure-linux-resource-matrix-evidence-20261028.json)。

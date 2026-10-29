# Codex 回执：原始 init 属性写入与读回

- 已通过：真实 property v2 写入 ACK、独立 getprop 读回及 `1、2、3` 连续值验证。
- 已通过：真实 SELinux 域转换、原始 linkerconfig/apexd bootstrap 返回、sysctl 恢复。
- 仍阻塞：服务启动期间 init 因不完整 APEX 闭包退出；PowerManager / DisplayManager 未就绪。
- 工程门：纯 Linux 1133 passed、1 skipped；公开主组 794 passed、27 skipped；主引擎 211 passed。
- GameApp Context、catalog、ResourceManager、GameState、Step 和完整对局继续未验收。

详见[阶段报告](PURE_LINUX_INIT_PROPERTY_READBACK_2026-10-29.md)和
[脱敏证据](pure-linux-init-property-readback-evidence-20261029.json)。

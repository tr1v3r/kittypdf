# kittypdf 架构审查与 Bug 排查报告

- **审计基线**：commit `02b0255`（v0.1.0）+ t1 键位改动（工作区未提交，见附录 A）
- **范围**：src/kittypdf/ 全部 5 模块（577 行）+ `__main__.py` + `pyproject.toml` + `README.md`
- **方法**：只读审计 + 行为实证复现（fake-Terminal 直驱 `_loop`、pymupdf 1.28.2 实测、pty 模拟器）+ kitty 源码（master 与 v0.29.2）及规范原文佐证、WezTerm apc.rs 交叉验证
- **结果**：**36 项 findings = 2 blocker / 2 high / 9 medium / 22 low / 1 info**；另有 20+ 维度「已检查无问题」（见 §5）
- **修复状态**：P0 批次（GFX-1/GFX-2/TERM-1，连带 GFX-10）已于 2026-09-19 修复并复核 PASS（见附录 C）；P1-P3 未修
- **核心结论**：分层骨架干净，但 **v0.1.0 在真 kitty 上完全不可用**（探测必失败 + 图像必被拒收，两个 blocker 相互独立）；回退模拟器掩盖了协议层缺陷。

## 1. 架构与数据流

```
【分层】kitty ⇄ term.py(tty门面: raw/SIGWINCH/解码) ⇄ app.py(_loop 状态机 + Reader)
                       │                                ├─ graphics.py(图形协议: probe/send/place/delete)
                       │                                └─ render.py(pymupdf: fit/反色/pixmap)
【翻页】e → _STEP_KEYS → step(+1,count) → draw()
        → sig 未变: 仅 place ─ 已传图复用
        → sig 变化: delete → render → send → place → status()
【错误路径】render 抛错 → status("render error") → _sent 不更新 → 下一帧重试（自愈不卡死）
【启动】enter(raw+备用屏) → probe(a=q) → 失败即退（消息打在备用屏上,切回主屏后被抹掉）
```

职责边界评价：term/graphics/render/app 四层依赖单向、纯 stdlib 门面 + 单一 pymupdf 依赖，**分层本身健康**；缺陷集中在两条契约线上：**kitty 协议合规**（graphics）与**输入/终端生命周期契约**（term × app 错误路径耦合）。

## 2. Blocker（真 kitty 上启动即死 / 页面永不显示）

| ID | 位置 | 问题 | 修复建议 |
|---|---|---|---|
| GFX-1 ✅已修(P0) | graphics.py:29,43 | `probe` 发 `a=q,s=1,v=1,i=1` **无 payload**。kitty 对 `a=q` 走完整加载校验：f 缺省 32(RGBA)×1×1 需 4 字节而载荷为空 → 回 `ENODATA`（不含 OK）→ probe 必 False → **app 在 kitty 里启动即退（exit 2）**。模拟器宽容未暴露 | 探测带最小载荷（`f=24,s=1,v=1;AAAA`），或以「任意 `_G` APC 响应」为支持判据 + DA1 限时 |
| GFX-2 ✅已修(P0) | graphics.py:48,54 | `f=100`(PNG) + `o=z` 但**缺规范强制要求的 `S` 键**。kitty 无 `S` 时按兜底 102400 字节精确分配 inflate 输出缓冲，PNG ≠ 该尺寸即 **EINVAL 拒收 → 即使过了探测页面也永不显示**。WezTerm 宽松、ghostty 贴 kitty 实现高风险 | 删 `o=z`（PNG 已 deflate，零收益）或补 `S=len(png_bytes)` |

> 修复顺序建议（auditor-graphics）：**GFX-2 → GFX-1 → GFX-3 → GFX-4**。

## 3. High

| ID | 位置 | 问题 | 修复建议 |
|---|---|---|---|
| GFX-3 | graphics.py（全部序列）+ term.py | `q=1` 只抑制 OK 响应，**错误响应照回**（kitty graphics.c:1028-1029，quiet>1 才全静默）。GFX-1/GFX-2 的失败 APC `\x1b_Gi=1;EINVAL:…\x1b\\` 泄回 stdin，实测被 term.py 解码成 **73 个幻影按键**（G=跳末页、i=反色×5、数字进 count）——协议错误直接变成随机翻页 | 全部改 `q=2`；term 层过滤 `_G` APC |
| TERM-1 ✅已修(P0) | term.py:127-133,163-169 | `read_key` 不传播 EOF：`os.read()==b""` 被当「无数据」；EOF 时 select 恒就绪 → **0.5s 超时窗内 `_read_more` 空转 4,157,646 次（实证）**，100% CPU 死循环、app 永不退出（pty 关闭输入侧即触发） | EOF 传播为显式事件/异常 |

## 4. Medium

| ID | 位置 | 问题 | 修复建议 |
|---|---|---|---|
| TERM-2 | term.py 解码器 | 跨 read 分裂的 UTF-8 字符被逐字节丢弃，残留续字节可连吞后续 2 个真实按键为 `('char','?')`（实证：'中' 分裂 + b"\xadjk" → 只剩一个 '?'）——违反模块 docstring 的「整段消费不泄漏」承诺；>4096B 粘贴在块边界遇 CJK 必现 | 增量 UTF-8 解码器 |
| TERM-3 | term.py enter() + app.py:140 | `enter()` 非失败原子，且 app 把 enter 放 try/finally 之外：tcsetattr 后 write/refresh_size 抛异常 → **终端留在 raw 模式** | enter 移入 try；或失败回滚 tcsetattr |
| GFX-4 | graphics.py:65-66 | `delete_image` 缺 `d=i`：kitty 与 WezTerm 对无 `d` 键的 `a=d` 均默认删全部 placement，`i` 被忽略——按 id 删除契约未成立，现靠全场单图巧合工作 | `a=d,d=i,i=id,q=2` |
| GFX-5 ≈ APP-1 | app.py:143-150 / 154-161 | 不支持终端时错误消息 print 在 raw+备用屏上，`term.exit()` 切回主屏（?1049l）把消息整屏抹掉——**用户零提示，只见无声 exit 2**（同根两条，修一处即可） | 先退出备用屏/恢复 tty 再打印错误 |
| GFX-6 | graphics.py probe | `b"OK" in resp` 子串判定脆弱：用户在 1s 探测窗内敲 "OK" 误判支持；错误响应（GFX-1、旧 Konsole）误判不支持 | 精确匹配响应结构而非子串 |
| RDR-1 | render.py:12-13 | **加密 PDF 漏检**：`pymupdf.open()` 对 AES-256 加密文档不抛错且 `page_count=1`（实测），绕过 app 的 0 页守卫；进 TUI 后 load_page 抛 "document closed or encrypted" → 状态栏死循环报错、黑屏，无密码通道 | `__init__` 检查 `needs_pass` 即刻报错退出（或 `authenticate(getpass())`） |
| APP-2 | __main__.py:4 | `python -m kittypdf` 丢弃 `main()` 返回值，**永远 exit 0**（实测：missing file 时 console script exit 1 / `python -m` exit 0） | `sys.exit(main())` |
| APP-3 | app.py:105-111 | 状态栏 left 无截断 + `len()`≠显示宽度：96 字符 render 错误消息在 40 列终端实测可见宽 109 → 底行 autowrap 滚动备用屏 → **kitty 图像随单元格错位** | 按显示宽度截断 left |

## 5. Low / Info（22 low + 1 info，明细按模块）

**term.py（5 low + 1 info）**：exit() 的 tcsetattr 未包异常（finally 里会掩盖原异常）；lone-ESC 提交留引入符残渣按 char 泄漏；非主线程 SIGWINCH fallback 注释承诺不存在的行为；零像素报告保留陈旧几何混合态；alt+key 刻意折叠为裸键；_raw_attrs 二次 tcgetattr/无二次 enter 防护。

**graphics.py（5 low）**：place 无 `p=1` → sig 未变时重复 place 累积重复 placement（app.py:89-92 配合）；探测窗吞用户按键且不回填 term._buf；place 无 `C=1`（光标被协议移到图像右下未定义区）；对 PNG 二次 zlib.compress 无收益（✅已随 GFX-2 修复一并移除）；delete_all 小写 `d=a` 不释放图像数据（退出路径可用 `d=A`）。

**render.py（3 low）**：Document 无 close/上下文管理器，fd 全程持有（lsof 实证；单文件 CLI 靠进程退出兜底）；反色 4 次样本拷贝（8MP 页瞬时 ~120MB；`pix.invert_irect()` 原地零拷贝；LUT 本身 256 双射正确已验证）；防御不一致——zoom 分母有 max(...,1) 而 `rect.width*rect.height` 无守卫（实测不可达，MuPDF 把空/负 MediaBox 替换为 Letter，一行 max() 消除隐式依赖）。

**app.py（9 low）**：`isdigit()` 陷阱——"²" 进 count 缓冲后 `int()` 无守卫崩溃（探针复现，app.py:200）；Progress.load 漏 TypeError/AttributeError（`{"page":[]}` 实测逃逸）；无 SIGTERM 清理（tty 留 raw）；其余 6 项明细见任务 output。

## 6. 已验证无问题（审计的另一半结论）

- **term.py**：raw 属性集、exit 幂等、take_resize 合并语义、除零防御、CSI/SS3 修饰键整段消费、PEP475 EINTR 安全、write flush 顺序（binary/text 实证无乱序）。
- **graphics.py**：q=1 在「压 OK」层面覆盖每条序列；chunk=4096（≤上限、4 倍数）、m=1/0 与首块控制键布局；zlib→base64 顺序；a=t+a=p 组合及块间无穿插；_IMAGE_ID 复用无竞态；delete_all 转义序列；place 1-based 与居中自洽（col/row 恒≥1）；8M 像素上限 ≪ kitty MAX_DATA_SZ 400MB。
- **render.py**（pymupdf 1.28.2，14k+ 组合实测）：等比缩放 min 数学；旋转页宽高交换（page.rect 已含 /Rotate）；ceil 不溢出视口（float32 吸收 epsilon）；_MAX_PIXELS cap 恰 8.00M；zoom floor 0.01 兜底；返回 (w,h) 与 pixmap 一致；0 页/伪 PDF/空文件/截断/越界页码全优雅处理。
- **app.py**：_loop count/pending_g 全路径无状态泄漏（含新键位）；_sent 缓存 render 失败自愈；invalidate/draw/status 各路径顺序完备；Progress 越界 clamp/不可写降级 OK；pyproject 入口与版本一致。

## 7. 修复路线图（建议批次）

```
P0 可用性    ✅已完成（2026-09-19，t7 复核 PASS）：GFX-2、GFX-1、TERM-1，连带 GFX-10
             真 kitty 冒烟待用户执行（附录 C）
P1 协议/契约 GFX-3(q=2+APC过滤) → GFX-4 → GFX-6 → RDR-1(加密PDF) → APP-2(exit code)
P2 错误可见性 GFX-5+APP-1 同根合修 → APP-3(状态栏截断) → TERM-3(enter 原子性)
P3 清理      SIGTERM、invert_irect 零拷贝、close 契约、p=1/C=1、TERM-2 UTF-8 解码器、
             low 全量（见 §5；TERM-2 严格说该进 P1，但触发面窄可缓）
回归基线     /tmp/kp_unit.py 11/11 + /tmp/kitty_emu3.py（注意：模拟器过宽容，
             修 GFX-1/2 后应同步收紧模拟器对 a=q 校验与 S 键的模拟，否则回归失真）
```

## 附录 A：本轮落地改动（t1，唯一代码变更）

colemak 键位改造（对齐用户 neovim 配置的 ueni 菱形）：`e/u`=下/上页主键、`E/U`=±10 页大跳（`_BIG_JUMP`，count 相乘）、`j/k/b`/空格保留别名、新增 `Q` 退出别名、`i` 反色保留（阅读器无水平导航，决策与理由已写进 README）、`n` 刻意不映射。改动仅 `src/kittypdf/app.py`（`_STEP_KEYS` 常量表 + dispatch 一行）与 `README.md`；验证 compileall + fake-Terminal 直驱 11/11（e/u/E/U/2e/3E/gg/G/q/别名/clamp/方向键/Ctrl+C/L/i/进度落盘）。auditor-app 对该改动复核 **PASS**（README↔代码逐行一致，最终代码复跑 11/11）。

## 附录 B：审计产出物

- 各模块完整结构化 findings（含未展开的 file:line 与修复建议）：团队 `kittypdf-arch-audit` 任务 output（t2/t3/t4/t5）
- 实证脚本（未入库）：/tmp/kp_unit.py（键位回归 11 用例 → P0 后扩至 21 用例）、/tmp/kp_audit.py（app 审计 11 探针）、/tmp/kitty_emu3.py（pty 模拟器）

## 附录 C：P0 修复记录（t6 实施 + t7 复核 PASS，2026-09-19）

- **GFX-2**：删 `o=z` 与 zlib import，`send_image` = 纯 base64 `f=100` 分块。复核依据：纯 f=100 路径下 kitty `inflate_png` 覆写 data_sz、png-reader 强制 RGBA 输出，required_sz 校验恒匹配（pymupdf RGB PNG 可接受）。
- **GFX-1**：probe 改发 `\x1b_Ga=q,f=24,s=1,v=1,i=1;AAAA\x1b\\`（3 字节载荷过 kitty 完整校验链 → 必回 OK）。不发探测图清理 delete：规范明文 a=q「nor will it store the image」，无物可删（t3 审计本就将该点归入无问题项）。
- **TERM-1**：`Terminal._eof` 粘性标志（select 就绪 + `os.read()==b""` 即置位），`read_key` 缓冲排空后返回 `("eof","")`，`_loop` 收到即存进度走正常退出路径（finally 恢复终端）。实测单 syscall 返回（修复前 0.5s 空转 415 万次）。
- **复核**（auditor-graphics，t7）：diff 逐行 + kitty graphics.c/png-reader.c 源码逐函数 + 独立字节级断言 + 21/21 独立复跑，五项全过；越界检查干净（place/delete 字节与基线一致，未夹带 P1-P3）。
- **回归**：/tmp/kp_unit.py 21 用例全过（键位 11 + P0 序列断言 10：probe 字节级、33-chunk 布局、EOF 单 syscall、main() 端到端 rc=0）。
- **真 kitty 冒烟（留给用户）**：①`PYTHONPATH=src python3 -m kittypdf /tmp/kp40.pdf` 应显示第 1 页，e/u/E/U/G/gg/q 可用（修复前启动即退）；②沙箱外 `python3 /tmp/kitty_emu3.py /tmp/kp-run /tmp/kp40.pdf /tmp/out.bin '2:e,4:q'`，收尾关 pty 应干净 exit=0（修复前 100% CPU 挂死）。注：raw 模式下 Ctrl+D 是 0x04 字符（`('ctrl','D')`）而非 VEOF，EOF 的触发面是 pty 关闭/挂断。
- **遗留**：仓库根 `uv.lock` 为审计实验副产物（项目不使用 uv），已清理/勿提交。

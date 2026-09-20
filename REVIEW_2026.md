# kittypdf 代码审查报告（2026 复审）

> **状态更新（2026-09-19 修复轮）**：本报告所述 3 个 medium（APP-2/F1/APP-3）及
> GFX-3/GFX-4/GFX-5/GFX-6/GFX-12/RDR-1/TERM-3/toc-hint/F4 已由 PR #12–#15
> squash 合入 main（`f575f67`），合并态回归 30/30 全绿。**仍遗留**：TERM-2
> （UTF-8 分裂逐字节丢失）、GFX-11（`C=1` 需 kitty ≥ 0.23，README 未标注）、
> reviewer 留观项（不完整 APC 帧滞留按键缓冲）。真 kitty 冒烟尚未执行，
> 重点：C=1 footer 不重复、透明模式、q=2 后无幻影按键。

- **审查基线**：main `3c71b7f`（含 PR #9–#11：透明纸张模式、footer 重绘修复）
- **对照基线**：`BUG_AUDIT.md`（commit `02b0255` 全量审计，36 findings，P0 已修）
- **方法**：三路并行审查（t1 graphics.py kitty 协议、t2 render/toc/app/CLI、t3 健壮性与安全），交叉对照 BUG_AUDIT.md，实测验证（unittest 16/16 OK，畸形输入/加密 PDF/退出码实测）
- **结果**：**0 blocker / 0 high / 3 medium / 若干 low-info**；PR #9–#11 新增代码质量良好，未引入新 blocker/high；BUG_AUDIT 的 P1/P2 遗留项**全部仍未修**（非复发，是一直 open）

## 1. 总体结论

PR #10/#11 本身实现正确：

- **PR #11 footer 修复正确**：`place()` 加 `C=1`（不移动光标），根因是 kitty 默认把光标推进一个虚拟占位符宽度 → wrap → 滚动备用屏 → footer 错位。单一放置路径全覆盖，新增字节级防回归测试。
- **透明模式（PR #9/#10）管线健全**：MuPDF alpha 语义正确；invert 与 transparent 组合走 `invert_irect()` 保 alpha 无光晕；sig/Progress/CLI 链路完整；t1 复核 `send_image` 维持纯 `f=100` 无 `o=z`（未破坏 P0 修复）。
- 回归：`.venv/bin/python -m unittest discover -s tests` 16/16 OK；畸形输入（garbage/empty/missing/user-pw 加密）均 stderr 清晰 + console-script exit 1，无 traceback。

但上轮审计的 P1/P2 修复路线图**未推进**（本次复确认仍 open），且本轮新增 3 条 medium 级健壮性问题（其中 APP-2 属遗留升级确认，F1/F2 为新确认）。

## 2. 与 BUG_AUDIT.md 的差异总表

| 状态 | 项 |
|---|---|
| ✅ 已修（P0，维持） | GFX-1（probe 带 f=24,s=1,v=1,i=1;AAAA）、GFX-2（删 o=z 纯 base64）、TERM-1（EOF 粘性标志）、GFX-10（连带） |
| ⬜ 遗留未修（非复发） | GFX-3、GFX-4、GFX-5≈APP-1、GFX-6、RDR-1、APP-2、APP-3、TERM-2、TERM-3，及 P3 全量 low |
| 🆕 本轮新增 | F1（cache 损坏崩溃，med）、GFX-11（C=1 老版本兼容，low）、GFX-12（probe 吞按键，low，并入 GFX-6 面）、toc hint 截断 low、F4（cache 无淘汰，low）、若干 info |

无复发项：P0 修复在 3c71b7f 上保持完好，PR #10/#11 未触碰已修序列。

## 3. Findings（按严重级排序）

### Medium（3）

| ID | 位置 | 问题 | 来源 |
|---|---|---|---|
| F1 🆕 | app.py:58 `Progress.load` | 对损坏 cache 无防护：`page` 非数字 → `int()` ValueError 崩溃，且发生在 `term.enter()` 之后（终端处于 raw/备用屏，崩溃路径 dirty exit）。上轮 low（漏 TypeError/AttributeError）实际更严重 | t3 |
| APP-2 | __main__.py:4 | 丢弃 `main()` 返回值，`python -m kittypdf` 永远 exit 0（实测）。BUG_AUDIT 遗留未修 | t2/t3 |
| APP-3 | app.py:213-231 `Reader.status()` | 状态栏 left 无显示宽度截断：窄终端 + 长 render error → 底行 autowrap 滚动 → 图像错位；新增 flags（a/s/d）加长 right 加剧预算紧张 | t2 |

### Low（关键项）

| ID | 位置 | 问题 | 状态 |
|---|---|---|---|
| GFX-3 | graphics.py 全部序列 | `q=1` 只压 OK，错误响应照回 stdin，可解码成幻影按键 | 遗留 |
| GFX-4 | graphics.py:65-66 | `delete_image` 缺 `d=i`，按 id 删除契约未成立 | 遗留 |
| GFX-6 (+GFX-12 🆕) | graphics.py probe | `b"OK" in resp` 子串判定脆弱；probe 1s 窗吞用户按键且不回填 `_buf` | 遗留+新增面 |
| GFX-5 ≈ APP-1 | app.py probe 失败路径 | 错误消息打印在备用屏，切回主屏被抹，无声 exit 2 | 遗留 |
| RDR-1 (+F3 细化) | render.py:12-13 | 加密 PDF 漏检；t3 细化：owner-pw-only（空用户密码）PDF `authenticate('')` 即可解锁但 render 不尝试 → 启动即拒，本可打开 | 遗留 |
| GFX-11 🆕 | graphics.py place `C=1` | C 控制键需 kitty 0.23+（2022）；老 kitty 若拒收则 place 整条失败。建议冒烟覆盖老版本或文档标注最低版本 | 新增 |
| TERM-3 | term.py enter() + app.py:140 | enter() 在 try/finally 外，异常时终端留 raw 模式 | 遗留 |
| TERM-2 | term.py:181-183 | 分裂 UTF-8 逐字节丢弃，可连吞后续按键 | 遗留 |
| F4 🆕 | ~/.cache/kittypdf | 无淘汰策略，永久增长 | 新增 |
| toc hint | toc.py `_draw` | `hint[:term.cols + 8]` 按 Python 字符而非显示宽度截断，CJK 截半/超宽 | 新增 |
| （info） | render.py | 渲染异常路径健壮：status 报错不崩、zoom 0.01 下限、`_MAX_PIXELS=8M` 上限、无临时文件遗留；README 与行为一致，仅 `R` 大写重绘别名未入键表 | 观察项 |

### 已验证无问题

- place 单一路径全覆盖（single/spread 左右/place-only 重放），重复 place 无副作用。
- 透明模式 × invert 组合正确；`_content_rect` 探测固定 opaque，autocrop 阈值语义稳定。
- `goto` 语义与 vim 一致；spread 单/双切换 clamp 正确。
- 畸形输入全路径优雅处理（stderr + exit 1，无 traceback）。

## 4. 建议修复优先级

```
P1（健壮性，小改动大收益）
  1. APP-2   sys.exit(main())                        — 一行
  2. F1      Progress.load try/except 宽口径吞损缓存  — 数行
  3. GFX-3   全部序列 q=2 + term 层过滤 _G APC        — 上轮即列 P1 首位
  4. GFX-4   delete 加 d=i
  5. GFX-6   probe 响应精确匹配（并入 GFX-12 吞按键问题）
  6. RDR-1   needs_pass 即刻报错 + 尝试 authenticate('')
P2（错误可见性/契约）
  7. GFX-5+APP-1 同根合修（错误消息移出备用屏再打印）
  8. APP-3   状态栏按显示宽度截断（顺带修 toc hint 同类问题）
  9. TERM-3  enter() 移入 try
P3（清理）
  TERM-2 增量 UTF-8 解码器、F4 cache 淘汰、GFX-11 老 kitty 兼容确认、
  其余 low（见 BUG_AUDIT §5）
```

另：**真 kitty 冒烟仍未执行**（BUG_AUDIT 附录 C 留给用户的命令）。C=1（GFX-11）与透明模式的真机表现应作为冒烟重点。

---
*审查团队：kittypdf-review-2（t1/t2/t3 并行审计，本报告为汇总）。详细过程见各任务 output 与 FINDINGS_t2.md。*

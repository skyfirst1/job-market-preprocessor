# 微信文章自动预处理

在项目目录执行：

```powershell
python -m jobprep wechat-run --url "https://mp.weixin.qq.com/s/ARTICLE" --no-ocr
python -m jobprep wechat-run --url "https://mp.weixin.qq.com/s/ARTICLE1" --url "https://mp.weixin.qq.com/s/ARTICLE2" --limit 2 --browser-channel msedge
python -m jobprep export
```

## 真实验证

当前入口不再固定文章 URL。大华 2027 招聘文章已在不操作任何桌面窗口的
情况下完成：1 次文章导航、3 次公开 CDN 图片请求、3 张原图均保存，
OCR 识别全部成功，得到 1,237 字。长图分片使用了 5 次通用 OCR 调用。
首次 `--no-ocr` 后再次执行默认命令只补本地图片 OCR；后续复跑没有新
导航或 OCR 调用。该分步验证不代表运行时需要手工分步，默认命令自动
执行抓取到 OCR/导出的完整流程。

结果位于 `exports/wechat/f5ced3db79eb9957fb46ee91/`，验证报告位于
`exports/evidence/wechat_automation_closure.json`。本地服务已重新加载新版
工具，`http://127.0.0.1:8765/tools` 的微信参数可检查默认值。监督窗口与
用户要求保留的证书不受这条路线影响。

前两篇新样本在 CDN 规则补齐前发现了不支持的图片来源，失败记录保留，
没有重新访问它们。上述成功证明通用 URL 流程可运行，不证明全公司岗位
或所有微信文章可抓取，故文档的整体 coverage 仍保守标为 partial。

请把示例 URL 换成公开文章地址。此命令使用 Workstation、worker_lock 和
WechatImage，通过 AppTools.wechat 自动采集正文与图片，不需要操作 GUI。
受微信访问限制、文章删除或挑战页面影响，不能保证所有文章均可抓取。
遇到验证、登录要求或其他挑战时停止，不绕过挑战，不自动重试。

公共采集工具还有独立硬限制：每个 artifact root 在滚动 24 小时内最多
采集 5 篇新文章；CLI 的单次 --limit 不会提高或重置这个额度。URL 持久
hash gate 防止重复导航，缓存复用时校验图片和 HTML 哈希。默认 15 秒
间隔跨进程生效，max_requests 累计计算浏览器与 CDN 请求。采集不干扰
已有认证状态或安全设置。没有 --force 或刷新入口，不删除 gate 来重新抓取。

图片 CDN 只允许两个精确主机：`mmbiz.qpic.cn` 和 `mmecoa.qpic.cn`，
并按各自主机的图片路径前缀校验。mmbiz 保留原有 /mmbiz_、/sz_mmbiz_
等受支持路径；mmecoa 仅支持 /mmecoa_、/sz_mmecoa_、/mmecoa/、/sz_mmecoa/。
协议相对图片地址补全为 HTTPS。这不是放开全部 qpic.cn 主机或任意 CDN。
下载失败保留对应图片记录和缺口，后续复用原结果，不重新访问文章补下载。

参数在读取配置、创建 Workstation 或调用采集工具前校验：

| 参数 | 默认值 | 范围 |
| --- | --- | --- |
| `--url` | 必填，可重复 | 最多 5 个公开 mp.weixin.qq.com/s 文章地址 |
| `--limit` | 1 | 1..5 |
| `--max-images` | 30 | 0..100 |
| `--max-requests` | 80 | 1..200 |
| `--interval` | 15 秒 | 1..300 秒 |
| `--browser-channel` | chrome | chrome / msedge |

URL 先通过 wechat_public.public_url 规范化：支持公开短链和包含 __biz/mid
的长链，去掉营销参数及 fragment，拒绝 key、uin、pass_ticket 等 session 参数。
全部 URL 校验成功后才调用 load_settings(root)，程序正常加载已配置的 .env
凭据，工具和终端报告不展示密钥。

所有显式 URL 都入队，规范化并去重后只处理前 limit 个，其余 ID 在
`deferred_task_ids` 中报告。只运行选中的显式任务，不导入 CSV，不处理
全队列或自动运行发现的链接。图片和请求上限传给采集工具，具体计数与
请求间隔由工具执行；站点 timeout 为 20 秒。

命令输出 JSON 摘要：`processed` 和 `results` 是本次 runner 结果；`cached`
包含缓存任务摘要、ocr_updated 和导出路径，`stopped` 包含无法安全继续的任务。
`exports` 与 `cached` 的 `images` 是图片记录数量，可能包含发现但未下载的图片；
`images_saved` 只统计有本地 path 且 status=ok 的图片，`image_complete` 沿用
采集 coverage 标记（未提供时为 false）。图片记录数量不代表下载成功数量。
报告不包含完整 result_json 或正文。重复 URL 优先复用数据库结果，不增加
attempts，不重新导航或下载图片。先验证文章身份、本地证据路径属于
data/artifacts、文件存在及大小、图片格式/尺寸和 SHA-256；缺失或损坏的
缓存停止并报告，不重新抓取，不将旧成功状态报告为有效缓存。

移除 `--no-ocr` 后，缓存有 OCR 缺失、ocr_gaps 或 ocr_skipped 时，使用现有
workstation.add_ocr 和持久预算对本地图片补识别，再 store.finish 保存结果。
完整成功的 OCR 直接复用；blocked、error、deleted 永远不补 OCR。补识别成功
清除 ocr_skipped，原 pending_ocr 状态恢复 acquisition_status。整个缓存操作
持有 worker_lock，不 claim 任务，即使 attempts 达到上限也可以补 OCR。
修改图片或请求上限不会更新已有采集证据。

选中任务自动写入 `exports/wechat/<taskid>/document.json` 和 `article.txt`。
document.json 保留证据字段并包含 preprocess_document 的 derived 视图；
article.txt 是 derived.compact_text（包括已有 OCR 文本）。`exports` 列出
各任务的摘要与文件路径。无法安全使用证据的任务导出失败摘要，不覆盖数据库
中的原始证据。需要全工作站审阅文件时另行运行 `python -m jobprep export`。

没有持久结果的显式任务必要时用 store.retry 重置状态，但始终
reset_attempts=False。保留原有 attempts 上限（默认 3）；达到上限时报告
attempt_budget_exhausted，不覆盖旧结果，也不重置预算。进程中断且尚未保存
result_json 时，防止已发出请求重复发送依赖公共采集工具的 durable gate；
该工具及 AppTools 集成由其他 worker 实现，CLI mock 测试不验证这个保证。
CLI 不改变 runner 实现，只对本命令关闭 automatic_retry 和等待重试。

`--no-ocr` 跳过付费 OCR；默认使用现有 OCR 引擎及其持久预算，配置上限
沿用 50 次，不重置已有消耗。配置加载和环境变量处理沿用 load_settings，
已持久化的 OCR 预算仍由引擎管理，不因再次运行而重置。

CLI 测试使用 mock Workstation 和本地临时 Store，不访问真实网络或付费 OCR。

# PixTagra 图库导出核验与实现

本轮源码根目录：`<project>`。
分支：`fix/audit-ui-20261028`；基准 commit：`f07dcdbef8a4fe12dceaf49d2b47828519528d43`。
工作区已有未提交修改；本轮保留它们，没有提交、回滚、切换分支或创建发布包。

本报告更新此前 `inventory-pilot-export-20261108.md` 中“格式待核实”的调查状态。
格式文档核对、文件读回、源码窗口验证与真实图库账号导入验收是不同层级；本轮没有上传用户图片或提交投稿。

## 已实现的下拉选项

位于图片工作区的“保存 / 导出”展开面板。选择网站后，导出按钮、可导出数量和拦截原因随实际规则更新。运行任务期间禁止切换或导出；切换网站或界面语言不会改写已有记录。

| 选项 | 实际输出 | 核实结果与必要人工步骤 |
| --- | --- | --- |
| Adobe Stock · CSV | 官方五列表头的 UTF-8 BOM CSV | Category、Releases 为可选字段，保持空白，在 Adobe 后台确认分类、绑定真实授权文件。 |
| Shutterstock · CSV | 官方七列表头的 UTF-8 BOM CSV | 英文元数据；先上传同名图片再导入 CSV；分类采用官方名称，自动推断仍需投稿人核对。 |
| Alamy · IPTC 图片 | 新目录中的 JPEG 副本、metadata.json、README.txt | 官方确认可读取 IPTC。英文短标题映射 Caption，完整描述另存 JSON；关键词保留顺序，原图和工作区保留。 |
| Getty/iStock · DeepMeta CSV | DeepMeta 4 支持的四列表头 CSV | 使用 DeepMeta 导入；英文标题、描述；本实现采用英文关键词，导入后必须核对 Getty 受控词匹配及歧义。 |
| 视觉中国 · 手动录入参考表 | 明确标记为参考用途的 CSV | **不是官方 CSV 导入模板，不能宣称直接上传。** 官方公开流程支持手动输入或选择系统推荐词；建议核心词为当前前五词，须本人在后台确认。 |

视觉中国的参考表字段为本应用定义的“文件名、标题、关键词（手动录入参考）、核心关键词建议（后台确认）、描述（参考）”。文件默认名含 `manual_reference_not_for_upload`，界面和导出结果提示均注明不可直接上传。没有伪造其 CSV 导入协议。

## 官方依据

### Adobe Stock

- [官方 CSV 上传样例](https://contributor.stock.adobe.com/static/csv/Sample_Adobe_Stock_CSV_upload.csv)
- [当前 CSV 格式要求](https://helpx.adobe.com/stock/contributor/manage-your-portfolio/csv-requirements-content.html)
- [创建 CSV 的操作说明](https://helpx.adobe.com/stock/contributor/manage-your-portfolio/create-csv-file.html)

确切表头：`Filename,Title,Keywords,Category,Releases`。UTF-8，最多 5000 行、1 MB；文件名含扩展名最多 30 字符；标题最多 70 字符，不含逗号或换行。当前 CSV 要求页面标明最多 50 个关键词，本轮据此执行；新生成记录仍受产品自身 20–35 个最终关键词规则限制。分类不能凭页面顺序臆造数字 ID，授权文件不能臆造。

原样模板留存于 `tests/fixtures/agency_exports/Sample_Adobe_Stock_CSV_upload.csv`，SHA-256：`41386ff0451921bdaa80b30ac91b9ed924e66f6d0598a97069a9d76a6e12aa62`。测试直接比较该文件表头。

### Shutterstock

- [CSV 与已有元数据的导入方法](https://submit.shutterstock.com/help/en/articles/10617486-how-do-i-include-existing-metadata-with-my-content-submission)
- [当前描述、关键词与分类规则](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata)
- [描述和关键词实践指南](https://submit.shutterstock.com/help/en/articles/10594702-description-and-keyword-best-practices)

确切列名：`Filename,Description,Keywords,Categories,Illustration,Mature Content,Editorial`。前四列必需，后三列为可选 Yes/No 标记；每图 1–2 个官方分类；英文描述上限 **2048 字符**，关键词 7–50 个。不能继续沿用旧资料的 200 字符上限。

旧实现中的 `Mature content` 大小写、yes/no 表示及 `Travel`、`Business and Finance`、`Architecture and Buildings`、`Objects and Concepts` 等分类与当前官方表不符。本轮调整为当前有效名称；保留原有基于已存在文本的自动分类流程，没有调用模型。Editorial/Illustration 等标记来自已有记录，不自动声称授权合规。

本机 Shutterstock 域名的系统 DNS 返回异常地址。调查通过腾讯公开 DNS 查询到该域名的官方 CloudFront CNAME，以该 CNAME 建立校验 TLS 的连接，并携带原官方 HTTP Host 读取公开帮助页；原 URL、传输 URL、Host、时间、状态与哈希保留于证据。没有修改系统 DNS，没有给生产应用添加网络绕行逻辑，没有使用账号或令牌。

### Alamy

- [官方图片管理、IPTC 与语言说明](https://www.alamy.com/help/contributor-image-management/)

官方确认读取 IPTC Caption、Keywords、拍摄日期，并要求英文 Caption。其“表格交由管理团队处理”不能等同于存在统一的自助 CSV 导入模板，因此本轮采用真实 IPTC JPEG 路径。

调用项目原有 `pixtagra_metadata.inject_metadata` 和独立读回校验器；验证写入结果和 JPEG 像素流；不重编码原图、不覆盖原图、不清空工作区。整批写入临时新目录，全部成功后才发布新输出目录；失败不发布半批结果。写入前后核对原图身份，拦截导出中被替换或删除的文件。

这不代表 Alamy 已审核通过图片分辨率、版权、授权书、地点、日期和 QC，投稿人仍需核对这些字段。

### Getty/iStock

- [DeepMeta 4 官方上传及 CSV 文档](https://www.deepmeta.com/blog/post/deepmeta-uploads)

使用文档明确列出的 `file name,title,description,keywords` 四列。英文标题与描述是官方要求。DeepMeta 允许独立配置关键词语言；本轮实现明确支持英文完整记录，没有声称 Getty 禁止所有非英文关键词。最多 50 个关键词，导入时 DeepMeta 查询受控词，投稿人需确认匹配结果。本轮没有把 DeepMeta 支持等同于所有 Getty 网页入口均支持相同文件。

### 视觉中国

- [视觉中国官方供稿招募页](https://static.vcg.com/html/register.html)
- [当前创作者中心合作指南](https://creatorstudio.500px.com.cn/faq)

实际检索链路：官方招募页 → 500px 摄影供稿入口 → 当前站点公开路由 → 创作者中心 `/faq` → 该公开网页加载的 FAQ 内容资源。当前旧版 FAQ 路由返回网页框架，不能据此认定其没有说明。本轮进一步取得公开内容脚本，解码后确认创意摄影的标题、关键词与核心词要求。

官方说明每图 5–35 个关键词、核心词选 1–5 个；填写方式为手动输入和系统推荐，两种可结合。其指南也说明标题应准确描述画面，地点须有事实依据。**没有取得官方 CSV 导入模板或 IPTC 自动读取承诺，因此不实现猜测的自动导入格式。**

本轮参考表可用于其已公开的手动填写流程；中文记录原样导出，英文记录也不会被自动翻译。不能把“指南使用中文示例”说成“官方要求全部关键词必须中文”。未修改生成提示词。

## 源码和测试变更

- `pixtagra_exports.py`：五个平台描述、格式校验、CSV 编码、Alamy 副本导出。
- `pixtagra.py`：平台下拉框、平台提示及拦截原因、选定平台导出、后台 JPEG 写入与主线程进度更新、Shutterstock 分类名修正。
- `pixtagra_localization.py`：新增控件及校验说明的中文翻译。
- `tests/test_agency_exports.py`、`tests/runtime/test_agency_export_workflow.py`：新增格式、边界、原图保护、失败回滚、真实控件与 100% / 225% 缩放测试。
- 原有导出/语言/TIFF/合规测试：保留测试目的，纠正旧的错误兼容性预期。中文记录不能再断言可直接导入 Shutterstock，改为明确验证其被拦截，并验证 Adobe CSV 与 JSON/JPEG 中文文本保真；旧分类和标记的断言改为官方当前要求。基础 SS 文件往返用例原只有 6 个词，补入有据的 `landscape` 达到官方 7 个的最小值。
- `tests/runtime/test_p1_02_metadata_save_ui.py`：原断言检查旧的 `CSV (blocked:` 文案，更新为多平台按钮的 `Export (blocked:`。未删除拦截断言或弱化质量规则。

所有新生成的 policy 3 记录，最终去重后不足 20 个关键词均被导出拦截，超过 35 个也被拦截；不能靠确认质量提示绕过。已有人工或旧版记录的原有行为受保护，同时仍须符合目标网站格式要求。没有改动 AI 生成、提示词、模型、API 接口、密钥、图片导入或通用元数据写入器。

## 验证与交付状态

唯一测试源码快照记录：`.build-rc/agency-export-20261108/frozen-source.json`，含 113 个源码/测试/构建输入文件的 SHA-256、分支、commit、未提交状态及真实入口。运行完整回归后必须再次比较这些哈希。

专项与完整回归的原始结果分别保留于 `targeted-tests.xml`、`full-tests.xml`；以同目录 `verification.json` 的最终状态和摘要为准，不能把早于最后修改的测试当作最后版本测试。

第一次完整回归为 908 通过、2 失败，原结果保留于 `full-tests-first-failed.xml`。一项是上述旧文案断言，另一项是原有视口用例观察到窗口 `iconic`、工作区不可见；没有据此修改生产视口代码或删除其可见性断言。修正文案后，两项定点复核均通过（`failure-audit.xml`），随后对最终固定源码重新运行完整回归。窗口曾被最小化的具体外部原因没有被证实，不能归咎于用户操作。

生产构建链：根目录 `pixtagra.py` → `PixTagra.spec` → `scripts/build_windows_rc.ps1` → `dist/build-a/PixTagra/PixTagra.exe`（A）或 `dist/PixTagra/PixTagra.exe`（B）。新增模块由入口静态导入，不需要猜测隐藏导入配置；本轮没有改构建链。

**本轮不执行构建或打包，也没有新的最终 EXE。** 之前真实图片关键词实验未通过用户规定的质量停止条件，该门槛仍然有效。旧 EXE 不含本轮下拉菜单，不能用于验收本轮源码改动。

验证等级最高为 **State B：源码测试与实际源码窗口验证**。尚未通过真实图库账号完成上传、导入或审核；尚无本轮确切 EXE 的启动与场景验收，不能声称 State D，也不能声称五个平台全部支持一键自动投稿。

最终完整回归：**910 passed，0 failed，0 skipped，173.55 秒**。全部 113 个固定输入哈希在测试后仍一致。State B；本轮付费 API 调用为 0，未读取或再次索要用户密钥，未构建、未打包、未向图库账号上传。

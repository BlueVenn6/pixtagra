"""Scoped UI translation adapters; model IDs and editor contents stay unchanged."""

import re
import tkinter as _tk
from tkinter import messagebox as _messagebox
import customtkinter as _ctk

from pixtagra_language import normalize_language

_language = "en"

ZH = {
    "Export": "导出",
    "Shutterstock · CSV": "Shutterstock · CSV",
    "Adobe Stock · CSV": "Adobe Stock · CSV",
    "Alamy · IPTC JPEG": "Alamy · IPTC 图片",
    "Getty/iStock · DeepMeta CSV": "Getty/iStock · DeepMeta CSV",
    "VCG · Manual entry reference": "视觉中国 · 手动录入参考表",
    "English metadata; upload the matching original filenames before the CSV.": "仅限英文元数据；先上传同名图片，再导入 CSV。",
    "Titles up to 70 characters; review category and releases in Adobe Stock.": "标题最多70字符；分类和授权文件需在 Adobe Stock 后台确认。",
    "English titles become captions in JPEG copies; originals and workspace are preserved.": "英文标题写入 JPEG 副本的说明字段；保留原图和工作区。",
    "English titles/descriptions; import with DeepMeta 4 and confirm keyword matches.": "英文元数据；通过 DeepMeta 4 导入并确认关键词匹配。",
    "Manual entry reference, not an upload template. Confirm 1–5 core keywords in VCG.": "此表供手动录入参考，不能直接上传；请在视觉中国后台确认1–5个核心词。",
    "VCG allows 5–35 keywords; choose 1–5 core keywords in the contributor portal.": "视觉中国允许5–35个关键词，请在供稿后台选定1–5个核心词。",
    "Export unavailable": "暂不可导出",
    "Correct these records before exporting.": "请修正这些记录后再导出。",
    "Some keywords need quality review. Continue export?": "部分关键词需要检查，是否继续导出？",
    "Duplicate filenames": "文件名重复",
    "Exports match images by filename. Rename images with duplicate filenames before exporting.": "导出文件通过文件名匹配图片，请先处理同名图片。",
    "Title, description and keywords are required.": "标题、描述和关键词不能为空。",
    "Keywords must be non-empty text values.": "每个关键词都必须是非空文本。",
    "One keyword contains a separator; split it into separate keywords before export.": "关键词中包含分隔符，请拆分为独立关键词后导出。",
    "Metadata contains a null character.": "元数据包含无效的空字符。",
    "This export requires English metadata. Existing text is not translated automatically.": "此导出要求英文元数据；已有内容不会自动翻译，请使用英文记录。",
    "Shutterstock requires 7–50 keywords.": "Shutterstock 要求7–50个关键词。",
    "Shutterstock descriptions allow at most 2048 characters.": "Shutterstock 描述最多2048字符。",
    "Adobe Stock filenames allow at most 30 characters including the extension.": "Adobe Stock 文件名包含扩展名最多30字符。",
    "Adobe Stock titles allow at most 70 characters.": "Adobe Stock 标题最多70字符。",
    "Adobe Stock CSV titles cannot contain commas or line breaks.": "Adobe Stock CSV 标题不能含逗号或换行。",
    "Adobe Stock export allows at most 50 keywords.": "Adobe Stock CSV 导出最多50个关键词。",
    "Getty/iStock allows at most 50 keyword terms.": "Getty/iStock 最多50个关键词。",
    "Alamy IPTC export accepts JPEG originals only; no image conversion is performed.": "Alamy IPTC 导出仅支持 JPEG 原图，请先移除其他格式的记录。",
    "Welcome to PixTagra": "欢迎使用 PixTagra", "Generation mode": "生成模式",
    "📷 General Photography": "📷 通用摄影", "🎨 Commercial Background": "🎨 商业背景",
    "Language / output": "界面与生成语言", "Creator (optional)": "作者（可选）",
    "Shoot location (optional)": "拍摄地点（可选）", "Date created (optional)": "拍摄日期（可选）",
    "Creator": "作者", "Shoot location": "拍摄地点", "Date created": "拍摄日期",
    "API Key": "API 密钥", "Load saved key": "加载已存密钥", "Save to Windows": "保存到 Windows",
    "Replace saved key": "替换已存密钥", "Clear session": "清除本次密钥", "Delete saved key": "删除已存密钥",
    "Base URL (optional):": "接口地址（可选）：", "Base URL:": "接口地址：", "Optional Base URL": "可选接口地址",
    "Optional custom endpoint / relay": "可选自定义接口或中转", "Local Server:": "本地服务：",
    "Migrate legacy keys": "迁移旧密钥", "Test Connection": "测试连接", "Session only": "仅本次使用",
    "📁 Import Images": "📁 导入图片", "↩ Resume Session": "↩ 恢复工作区", "Pick a date": "选择日期",
    "⌂ Home": "⌂ 首页", "Home": "首页", "Add images": "添加图片", "Generate metadata": "生成元数据",
    "Save / Export": "保存 / 导出", "Save / Export ▴": "保存 / 导出 ▴", "Save / Export ▾": "保存 / 导出 ▾",
    "Cancel": "取消", "⏹ Cancel": "⏹ 取消", "Image copies:": "图片副本：", "Metadata files:": "元数据文件：",
    "Save selected": "保存选中", "Save ready": "保存就绪", "Save all": "保存全部", "Save to folder": "保存到文件夹",
    "Mode:": "模式：", "Model:": "模型：", "Visual review (+1 API call per image)": "视觉复核（每图增加 1 次调用）",
    "Visual review (extra API calls)": "视觉复核（可能追加调用）",
    "Visual keyword check not run: AI metadata requires manual review": "未执行视觉复核：AI生成内容需要人工检查",
    "Ready": "就绪", "No records loaded": "尚未导入图片", "No preview": "暂无预览", "Select all": "全选", "Deselect": "取消选择",
    "All records passed. Ready to save.": "全部记录已通过检查，可以保存。",
    "Automated checks passed. Ready to save; review AI content before submission.": "自动检查已通过，可以保存；投稿前请人工核对AI内容。",
    "Add keywords": "添加关键词", "Selection / retry": "选择 / 重试", "Selection / retry ▾": "选择 / 重试 ▾",
    "Selection / retry ▴": "选择 / 重试 ▴", "Clear": "清空", "Remove selected": "移除选中",
    "Retry": "重试", "Retry failed": "重试失败项", "Retry selected": "重试选中项", "Select failed": "选择失败项",
    "Regenerate failed": "重新生成失败项",
    "Confirm retry": "确认重试", "Retry generation": "重新生成", "Save with review": "检查后保存",
    "Estimated requests: review starts at 1/image; generation may also need repairs and visual review.": "预计调用：仅复核从每图 1 次开始；生成还可能需要修复与视觉复核。",
    "Network retries may add requests and additional API usage or charges.": "网络重试可能增加调用次数、API 用量和费用。",
    "Existing generated text is reused when only visual review is incomplete.": "仅视觉复核未完成时，会复用已有生成文本。",
    "Select ready": "选择就绪项", "Select needs review": "选择待检查项", "View keyword check": "查看关键词复核",
    "Title (8-12 words)": "标题（8–12 个英文词）", "Description (30-50 words)": "描述（30–50 个英文词）",
    "Title (8-30 Chinese characters)": "标题（8–30 个汉字）", "Description (30-120 Chinese characters)": "描述（30–120 个汉字）",
    "Keywords (SEO tags, spam filtered)": "关键词（过滤重复与无效词）", "Keywords": "关键词",
    "Keywords (20–35 suggested)": "关键词（建议20–35个）", "20–35 tags": "建议20–35个",
    "↻ Regenerate": "↻ 重新生成", "Undo generation": "撤销生成", "× Remove": "× 移除", "Expand editor": "展开编辑器",
    "● Unprocessed": "● 未处理", "● Ready": "● 就绪", "● Needs review": "● 待检查", "● Failed": "● 失败",
    "● Saved": "● 已保存", "● Saved — needs review": "● 已保存，仍需检查", "● Manual edits pending": "● 有人工修改", "● Source changed / missing": "● 原图改变或缺失",
    "Keyword editor": "关键词编辑器", "English keywords": "英文关键词", "Add": "添加", "Apply": "应用",
    "Deduplicate": "去除重复", "Use cases last": "用途词后置", "Copy all": "复制全部", "Copy": "复制",
    "Cut": "剪切", "Paste": "粘贴", "Add to selected": "添加到选中图片", "Today": "今天",
    "Choose date": "选择日期", "Regenerate all": "全部重新生成", "Only keywords": "仅重新生成关键词",
    "Keep manual fields": "保留人工修改字段", "Choose what Regenerate should replace:": "选择重新生成的范围：",
    "This record has unsaved manual edits.": "这张图片有尚未保存的人工修改。",
    "Edit manually": "手动编辑", "Open record": "打开记录", "Save Shutterstock CSV": "保存 Shutterstock CSV",
    "Checking model connection...": "正在检查模型连接…", "Generation not started: connection check failed.": "未开始生成：连接检查失败。",
    "Generation not started: workspace unavailable. See the recovery message.": "未开始生成：工作区不可用，请查看恢复提示。",
    "Cancelling: no new images; waiting for active requests and retaining their results...": "正在取消：等待当前请求结束并保留结果…",
    "Closing after active requests / writes finish; progress is being retained...": "当前请求或写入结束后关闭，正在保留进度…",
    "Home returns to the start screen without clearing the current session.": "返回首页会保留当前工作区。",
    "These are quality warnings. You may save them as-is and accept the tradeoff.": "这些项目存在质量提示，请检查后决定是否保留并保存。",
    "Busy": "正在处理", "Info": "提示", "Error": "错误", "Warning": "警告", "Success": "成功", "Done": "完成",
    "Export Error": "导出失败", "Connection Check Failed": "连接检查失败", "Source changed": "原图已改变",
    "No ready images found": "没有已就绪的图片", "No ready metadata found": "没有已就绪的元数据",
    "No saveable images found": "没有可保存的图片", "Save is already in progress.": "正在保存，请稍候。",
    "Select Images": "选择图片", "Choose where to create the output folder": "选择输出文件夹的位置",
    "Settings load failed; safe defaults are active": "设置读取失败，已使用安全默认设置",
    "⚡ English-only stock metadata tuned for Getty, Shutterstock, Adobe Stock, and iStock": "⚡ 支持中英文图库元数据，按目标图库要求选择语言",
    "⚡ Stable cloud + relay + Ollama routing with EXIF + XMP + IPTC metadata writeback": "⚡ 支持云端、中转及 Ollama，写入 EXIF、XMP 和 IPTC 元数据",
    "Start with 50–100 images per batch; workspace limit 500.": "建议每批 50–100 张，工作区上限 500 张。",
    "Metadata language follows your selection; existing records keep their own language.": "生成语言由你选择，已有记录保留原语言。",
    "English": "English", "简体中文": "简体中文",
    "January": "一月", "February": "二月", "March": "三月", "April": "四月", "May": "五月", "June": "六月",
    "July": "七月", "August": "八月", "September": "九月", "October": "十月", "November": "十一月", "December": "十二月",
    "Mo": "一", "Tu": "二", "We": "三", "Th": "四", "Fr": "五", "Sa": "六", "Su": "日",
    "Metadata quality check": "元数据质量检查", "Manual edits detected": "检测到人工修改",
    "Visual keyword check": "关键词视觉复核", "Keyword quality review": "关键词质量检查",
    "English-only stock metadata": "英文图库元数据", "No processed images found.": "没有已处理的图片。",
    "Run AI analysis first.": "请先生成元数据。", "Metadata quality check failed": "元数据质量检查未通过",
    "Correct or regenerate these records before CSV export.": "请修正或重新生成这些记录，再导出 CSV。",
    "Some keywords need quality review. Continue CSV export?": "部分关键词需要检查，是否继续导出 CSV？",
    "Save ready only": "仅保存就绪项", "Save with warnings": "保留提示并保存", "Retry selected records": "重试选中记录",
    "Select with errors": "选中错误项", "Select missing metadata": "选中缺少元数据项",
    "Use cases last": "用途词后置", "Select All": "全选", "Delete": "删除",
    "Checking connection...": "正在检查连接…", "Connection check finished": "连接检查结束",
    "Load saved key with PIN": "使用 PIN 加载已存密钥", "Saved key loaded for session": "已加载密钥供本次使用",
    "Saved in Windows": "已保存到 Windows", "Legacy config key found - migrate explicitly": "发现旧配置密钥，请手动迁移",
    "Restart PixTagra to verify and complete migration": "请重启 PixTagra 以验证并结束迁移",
    "Legacy migration complete": "旧密钥迁移结束", "Migration requires attention": "密钥迁移需要检查",
    "Retry review": "重试待检查项", "📁 Save to folder": "📁 保存到文件夹", "📂 Save to folder": "📂 保存到文件夹",
    "Select error": "选中错误项", "Select unprocessed": "选中未处理项", "Select review": "选中待检查项",
    "Select missing": "选中缺少元数据项", "Batch keywords": "批量添加关键词", "Add keywords to selected": "为选中图片添加关键词",
    "Your name": "作者姓名", "City, country": "城市、国家", "YYYY": "年份",
}

_PATTERNS = [
    (re.compile(r"^Export \(blocked: (\d+)\)$"), r"导出（\1 条不符合要求）"),
    (re.compile(r"^Export \((\d+); review: (\d+)\)$"), r"导出（\1 条；\2 条待检查）"),
    (re.compile(r"^Export \((\d+)\)$"), r"导出（\1）"),
    (re.compile(r"^Generated metadata needs 20–35 final keywords; found (\d+)\.$"), r"生成记录必须有20–35个最终关键词；当前为\1个。"),
    (re.compile(r"^(\d+) tags$"), r"\1 个关键词"),
    (re.compile(r"^(\d+) total \| (\d+) selected$"), r"共 \1 张 | 已选 \2 张"),
    (re.compile(r"^(\d+) unprocessed \| (\d+) ready to save \| (\d+) need review \| (\d+) failed$"), r"\1 未处理 | \2 可保存 | \3 待检查 | \4 失败"),
    (re.compile(r"^(\d+) ready to save \| (\d+) need review \| (\d+) failed$"), r"\1 可保存 | \2 待检查 | \3 失败"),
    (re.compile(r"^Retry (\d+)$"), r"重试 \1 张"),
    (re.compile(r"^Retry (\d+) image\(s\)\?$"), r"重试 \1 张图片？"),
    (re.compile(r"^(\d+) record\(s\) are ready to save\.$"), r"\1 条记录可以保存。"),
    (re.compile(r"^Loaded (\d+) images$"), r"已导入 \1 张图片"),
    (re.compile(r"^Processing: (\d+)/(\d+)$"), r"正在处理：\1/\2"),
    (re.compile(r"^Saving: (\d+)/(\d+)$"), r"正在保存：\1/\2"),
    (re.compile(r"^Saved: (\d+) images, (\d+) failed$"), r"已保存 \1 张，失败 \2 张"),
    (re.compile(r"^📦 Start with 50–100 images per batch; workspace limit: (\d+)\.$"), r"📦 建议每批 50–100 张，工作区上限 \1 张。"),
    (re.compile(r"^(\d+) tags · (\d+)–(\d+) allowed; relevance first$"), r"\1 个关键词 · 允许 \2–\3 个，以相关性为先"),
    (re.compile(r"^(\d+) tags · (\d+)–(\d+) suggested; relevance first$"), r"\1 个关键词 · 建议\2–\3个，精准优先"),
    (re.compile(r"^Exported (\d+) items$"), r"已导出 \1 条记录"),
    (re.compile(r"^(\d+) record\(s\) need review\.$"), r"\1 条记录需要检查。"),
    (re.compile(r"^(\d+) hard error\(s\) must be fixed before saving\.$"), r"保存前必须修正 \1 个错误。"),
    (re.compile(r"^Loading (\d+)/(\d+) images\.\.\.$"), r"正在导入图片：\1/\2…"),
    (re.compile(r"^Saving (\d+) images\.\.\.$"), r"正在保存 \1 张图片…"),
    (re.compile(r"^Done: (\d+) ok, (\d+) review, (\d+) failed$"), r"处理结束：\1 就绪，\2 待检查，\3 失败"),
    (re.compile(r"^Cancelled: (\d+) ok, (\d+) review, (\d+) failed$"), r"已取消：保留 \1 就绪，\2 待检查，\3 失败"),
    (re.compile(r"^Cancelling — retaining active results: (\d+)/(\d+)$"), r"正在取消并保留当前结果：\1/\2"),
    (re.compile(r"^🔄 Retry selected \((\d+)\)$"), r"🔄 重试选中项（\1）"),
    (re.compile(r"^🔄 Retry failed \((\d+)\)$"), r"🔄 重试失败项（\1）"),
    (re.compile(r"^Regenerate failed \((\d+)\)$"), r"重新生成失败项（\1）"),
    (re.compile(r"^🔄 Retry review \((\d+)\)$"), r"🔄 重试待检查项（\1）"),
    (re.compile(r"^Local model not found\. On the Ollama server, install it with `ollama pull (.+)` and retry\.$"), r"未找到本地模型。请在 Ollama 服务所在设备执行 `ollama pull \1` 后重试。"),
    (re.compile(r"^Title has (\d+) Chinese characters \(expected 8-30\)$"), r"标题有 \1 个汉字，需要 8–30 个"),
    (re.compile(r"^Description has (\d+) Chinese characters \(expected 30-120\)$"), r"描述有 \1 个汉字，需要 30–120 个"),
    (re.compile(r"^Description has (\d+) sentences \(expected 2-3\)$"), r"描述有 \1 句，需要 2–3 句"),
]


def translate(text, language=None):
    if not isinstance(text, str) or normalize_language(language or _language) == "en":
        return text
    if text in ZH:
        return ZH[text]
    for pattern, replacement in _PATTERNS:
        if pattern.fullmatch(text):
            return pattern.sub(replacement, text)
    match = re.fullmatch(r"(.+?)( \([\d/]+\)| [▴▾])", text)
    if match and match[1] in ZH:
        return ZH[match[1]] + match[2]
    if "\n" in text:
        return "\n".join(translate(line, language) for line in text.split("\n"))
    return text


def set_ui_language(language, root=None):
    global _language
    _language = normalize_language(language)
    if root is not None:
        def visit(widget):
            if isinstance(widget, (_TextTranslation, LocalizedOptionMenu, LocalizedMenu, LocalizedEntry, NativeToplevel)):
                widget.refresh_translation()
            for child in widget.winfo_children():
                visit(child)
        visit(root)


class _TextTranslation:
    def __init__(self, *args, **kwargs):
        self._ui_text_source = kwargs.get("text")
        if "text" in kwargs:
            kwargs["text"] = translate(kwargs["text"])
        super().__init__(*args, **kwargs)

    def configure(self, *args, **kwargs):
        if "text" in kwargs:
            self._ui_text_source = kwargs["text"]
            kwargs["text"] = translate(kwargs["text"])
        return super().configure(*args, **kwargs)

    config = configure

    def refresh_translation(self):
        if self._ui_text_source is not None:
            super().configure(text=translate(self._ui_text_source))


class LocalizedLabel(_TextTranslation, _ctk.CTkLabel): pass
class LocalizedButton(_TextTranslation, _ctk.CTkButton): pass
class LocalizedCheckBox(_TextTranslation, _ctk.CTkCheckBox): pass
class NativeLabel(_TextTranslation, _tk.Label): pass
class NativeButton(_TextTranslation, _tk.Button): pass


class LocalizedEntry(_ctk.CTkEntry):
    def __init__(self, *args, **kwargs):
        self._ui_placeholder = kwargs.get("placeholder_text")
        if self._ui_placeholder is not None:
            kwargs["placeholder_text"] = translate(self._ui_placeholder)
        super().__init__(*args, **kwargs)

    def configure(self, *args, **kwargs):
        if "placeholder_text" in kwargs:
            self._ui_placeholder = kwargs["placeholder_text"]
            kwargs["placeholder_text"] = translate(self._ui_placeholder)
        return super().configure(*args, **kwargs)

    def refresh_translation(self):
        if self._ui_placeholder is not None:
            super().configure(placeholder_text=translate(self._ui_placeholder))


class NativeToplevel(_tk.Toplevel):
    def title(self, value=None):
        if value is None:
            return super().title()
        self._ui_title = value
        return super().title(translate(value))

    def refresh_translation(self):
        if hasattr(self, "_ui_title"):
            super().title(translate(self._ui_title))


class LocalizedOptionMenu(_ctk.CTkOptionMenu):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refresh_translation()

    def refresh_translation(self):
        if not hasattr(self, "_dropdown_menu"):
            return
        text = translate(self._current_value)
        if self._text_label.cget("text") != text:
            self._text_label.configure(text=text)
        values = tuple(translate(value) for value in self._values)
        if getattr(self, "_translated_values", None) != values:
            self._dropdown_menu.configure(values=list(values), command=self._dropdown_callback)
            self._translated_values = values

    def _dropdown_callback(self, value):
        canonical = next((source for source in self._values if translate(source) == value), value)
        super()._dropdown_callback(canonical)
        self.refresh_translation()

    def _variable_callback(self, *args):
        super()._variable_callback(*args)
        self.refresh_translation()

    def set(self, value):
        super().set(value)
        self.refresh_translation()

    def configure(self, *args, **kwargs):
        # CTk replaces the menu's choices with canonical values even when the
        # same values are supplied again. Restore their current translation.
        if "values" in kwargs:
            self._translated_values = None
        super().configure(*args, **kwargs)
        self.refresh_translation()


class LocalizedMenu(_tk.Menu):
    def __init__(self, *args, **kwargs):
        self._ui_entries = {}
        super().__init__(*args, **kwargs)

    def add_command(self, **kwargs):
        source = kwargs.get("label", "")
        kwargs["label"] = translate(source)
        super().add_command(**kwargs)
        self._ui_entries[self.index("end")] = source

    def refresh_translation(self):
        for index, source in self._ui_entries.items():
            self.entryconfigure(index, label=translate(source))


class _ModuleView:
    def __init__(self, module, overrides):
        self._module, self._overrides = module, overrides

    def __getattr__(self, name):
        return self._overrides.get(name, getattr(self._module, name))

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        elif name in self._overrides:
            self._overrides[name] = value
        else:
            setattr(self._module, name, value)


class _MessageBoxes:
    def __getattr__(self, name):
        operation = getattr(_messagebox, name)
        def show(*args, **kwargs):
            args = tuple(translate(value) if index < 2 else value for index, value in enumerate(args))
            for key in ("title", "message", "detail"):
                if key in kwargs:
                    kwargs[key] = translate(kwargs[key])
            return operation(*args, **kwargs)
        return show


ctk = _ModuleView(_ctk, {"CTkLabel": LocalizedLabel, "CTkButton": LocalizedButton, "CTkCheckBox": LocalizedCheckBox, "CTkOptionMenu": LocalizedOptionMenu, "CTkEntry": LocalizedEntry})
tk = _ModuleView(_tk, {"Label": NativeLabel, "Button": NativeButton, "Menu": LocalizedMenu, "Toplevel": NativeToplevel})
messagebox = _MessageBoxes()

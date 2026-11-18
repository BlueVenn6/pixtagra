# Claude Code 精确修改指令 — Picture SEO Studio

**文件：** `picture_seo_329.py`  
**改动范围：** 三个独立功能块，互不依赖，按顺序执行  
**禁止操作：** 不得修改 `inject_metadata` 以外的现有 JPEG 写入逻辑；不得改动 `clean_keywords`、`build_prompt`、`_prompt_general_photography`、`_prompt_commercial_background`

---

## 修改一：缩略图加载性能优化（解决 50–100 图卡顿）

### 根因分析

`load_thumb` → `load_image_bytes` 读取完整文件字节（对 10MB RAW 导出 JPEG 可达数十 MB），
再用 Pillow 解码全图、缩小到 140×140，然后才显示。50 张图 = 50 次全量 IO + 解码，
全部并发进 `_thumb_pool`（4 线程），在慢速机械盘上 4 线程同时读文件反而比顺序读慢（磁头争用）。

此外 `_load_batch` 每批都调用 `_refresh_scroll()`，后者 `update_idletasks()` 触发全量布局重算，
每 50ms 一次，100 张 = 50 次强制重排。

### 修改 A：`load_thumb` — 优先读 EXIF 嵌入缩略图

**找到这段代码（约 1402–1420 行）：**

```python
def load_thumb(self):
    if self.thumb_loaded:
        return
    self.thumb_loaded = True
    def _load():
        data = load_image_bytes(self.path)
        if data:
            try:
                with Image.open(io.BytesIO(data)) as src:
                    src.thumbnail((140, 140), Image.Resampling.LANCZOS)
                    img = src.copy()
                ctk_img = ctk.CTkImage(light_image=img, size=img.size)
                def _apply_thumb(i=ctk_img):
                    self.thumb_image = i
                    self.img_lbl.configure(image=i, text="")
                self.after(0, _apply_thumb)
            except Exception:
                pass
    self.app._thumb_pool.submit(_load)
```

**替换为：**

```python
def load_thumb(self):
    if self.thumb_loaded:
        return
    self.thumb_loaded = True
    def _load():
        img = None
        # ── 优先路径：读 EXIF 内嵌缩略图（通常 < 5KB，IO 极快）──
        try:
            exif_data = piexif.load(self.path)
            thumb_bytes = exif_data.get("thumbnail")
            if thumb_bytes and len(thumb_bytes) > 100:
                with Image.open(io.BytesIO(thumb_bytes)) as t:
                    t.thumbnail((140, 140), Image.Resampling.LANCZOS)
                    img = t.copy()
        except Exception:
            pass
        # ── 回退路径：读完整文件，但只读到足以解码缩略图的前 64KB ──
        if img is None:
            try:
                path = self.path
                # 用 Pillow 惰性解码（不读全图像素）
                with Image.open(path) as src:
                    src.draft("RGB", (140, 140))  # 让 JPEG 解码器降分辨率
                    src.thumbnail((140, 140), Image.Resampling.LANCZOS)
                    img = src.copy()
            except Exception:
                return
        if img is None:
            return
        ctk_img = ctk.CTkImage(light_image=img, size=img.size)
        def _apply_thumb(i=ctk_img):
            self.thumb_image = i
            self.img_lbl.configure(image=i, text="")
        self.after(0, _apply_thumb)
    self.app._thumb_pool.submit(_load)
```

> **关键说明：**  
> - `piexif.load(path)` 直接读文件头，通常只读几 KB，极快  
> - `Image.open(path)` 不加 `io.BytesIO(data)` 参数，避免先把整个文件读进内存  
> - `.draft("RGB", (140, 140))` 告诉 JPEG 解码器在 DCT 阶段就降采样，内存占用减少 8×

---

### 修改 B：`_load_batch` — 延迟 scroll 重算，批次改为 5

**找到这段代码（约 1839–1862 行）：**

```python
def _load_batch(self, paths, idx):
    """Create image cards in small batches to keep UI responsive."""
    BATCH = 2  # cards per tick — small enough to avoid any stutter
    end = min(idx + BATCH, len(paths))
    for i in range(idx, end):
        p = paths[i]
        if p not in self.cards:
            card = ImageCard(self.scroll_frame, p, self)
            self.cards[p] = card
            card.load_thumb()  # threaded — won't block
    
    self._refresh_scroll()  # update scroll region after each batch
    
    if end < len(paths):
        self.stats["total"] = len(self.cards)
        self._update_stats()
        self.prog_lbl.configure(text=f"Loading {end}/{len(paths)} images...")
        self.after(50, lambda: self._load_batch(paths, end))  # 50ms yield
    else:
        # All loaded
        self.stats["total"] = len(self.cards)
        self._update_stats()
        self.prog_lbl.configure(text=f"Loaded {len(paths)} images")
        self._save_config()
```

**替换为：**

```python
def _load_batch(self, paths, idx):
    """Create image cards in small batches to keep UI responsive."""
    BATCH = 5  # 5 cards per tick — 每张卡片 IO 已大幅减轻，可提高批次
    end = min(idx + BATCH, len(paths))
    for i in range(idx, end):
        p = paths[i]
        if p not in self.cards:
            card = ImageCard(self.scroll_frame, p, self)
            self.cards[p] = card
            card.load_thumb()  # threaded — won't block
    
    # 只在最后一批或每 20 张更新一次 scroll，避免频繁重排
    if end >= len(paths) or (end % 20 == 0):
        self._refresh_scroll()
    
    if end < len(paths):
        self.stats["total"] = len(self.cards)
        self._update_stats()
        self.prog_lbl.configure(text=f"Loading {end}/{len(paths)} images...")
        self.after(30, lambda: self._load_batch(paths, end))  # 30ms yield
    else:
        # All loaded — 最终强制刷新一次
        self._refresh_scroll()
        self.stats["total"] = len(self.cards)
        self._update_stats()
        self.prog_lbl.configure(text=f"Loaded {len(paths)} images")
        self._save_config()
```

---

## 修改二：Getty Images — Date Created + Country 自动注入

### 概述

在 App 顶部工具栏增加「拍摄地点」和「拍摄日期」全局输入框。
保存时将这两个值写入：
- IPTC 2:101 Country/Primary Location Name  
- IPTC 2:55 Date Created（格式 YYYYMMDD）  
- EXIF ExifIFD.DateTimeOriginal（格式 YYYY:MM:DD HH:MM:SS）  
- XMP `photoshop:DateCreated` 和 `Iptc4xmpCore:CountryName`  

### 修改 C：`App.__init__` — 添加两个新变量

**找到这段代码（约 1556–1562 行）：**

```python
        self.cards, self.data_cache = {}, {}
        self.is_processing, self.stop_flag, self.showing_ws = False, False, False
        self.is_saving = False
        self.error_lock = threading.Lock()
        self.api_errors = []
        self.stats = {"total": 0, "processed": 0, "success": 0, "failed": 0}
        self._thumb_pool = ThreadPoolExecutor(max_workers=4)
```

**替换为：**

```python
        self.cards, self.data_cache = {}, {}
        self.is_processing, self.stop_flag, self.showing_ws = False, False, False
        self.is_saving = False
        self.error_lock = threading.Lock()
        self.api_errors = []
        self.stats = {"total": 0, "processed": 0, "success": 0, "failed": 0}
        self._thumb_pool = ThreadPoolExecutor(max_workers=4)
        # Getty 元数据字段
        self.shoot_location_var = tk.StringVar()   # 拍摄地点 → IPTC Country
        self.shoot_date_var = tk.StringVar()        # 拍摄日期 → EXIF DateTimeOriginal
```

---

### 修改 D：`_setup_welcome` — 在顶部工具栏加入两个输入框

**找到这段代码（约 1589–1600 行，即 Creator 那行的下方）：**

```python
        self.creator_var = tk.StringVar()
        ctk.CTkEntry(cfg, textvariable=self.creator_var, placeholder_text="Creator", width=150, height=40, border_color="#597D87", border_width=1, corner_radius=20).pack(side="left", padx=(0, 15))
```

**替换为：**

```python
        self.creator_var = tk.StringVar()
        ctk.CTkEntry(cfg, textvariable=self.creator_var, placeholder_text="Creator", width=150, height=40, border_color="#597D87", border_width=1, corner_radius=20).pack(side="left", padx=(0, 15))

        # ── Getty: 拍摄地点 + 拍摄日期（全局，写入所有保存图片的元数据）──
        ctk.CTkEntry(cfg, textvariable=self.shoot_location_var, placeholder_text="📍 Location (Getty)", width=160, height=40, border_color="#8B5CF6", border_width=1, corner_radius=20).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(cfg, textvariable=self.shoot_date_var, placeholder_text="📅 Date YYYY-MM-DD", width=155, height=40, border_color="#8B5CF6", border_width=1, corner_radius=20).pack(side="left", padx=(0, 15))
```

---

### 修改 E：`_load_config` 和 `_save_config` — 持久化新字段

**找到 `_save_config` 中的 `json.dump` 调用（约 1750–1758 行）：**

```python
            json.dump(
                    {
                        "creator": self.creator_var.get(),
                        "model": self.model_var.get(),
                        "mode": self.mode_var.get()
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
```

**替换为：**

```python
            json.dump(
                    {
                        "creator": self.creator_var.get(),
                        "model": self.model_var.get(),
                        "mode": self.mode_var.get(),
                        "shoot_location": self.shoot_location_var.get(),
                        "shoot_date": self.shoot_date_var.get(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
```

**找到 `_load_config` 末尾、 `except Exception: pass` 之前（约 1737–1742 行）：**

```python
                self.creator_var.set(cfg.get("creator", ""))
                if cfg.get("model") in API_CONFIG:
                    self.model_var.set(cfg["model"])
                if cfg.get("mode") in PROMPT_MODES:
                    self.mode_var.set(cfg["mode"])
```

**替换为：**

```python
                self.creator_var.set(cfg.get("creator", ""))
                self.shoot_location_var.set(cfg.get("shoot_location", ""))
                self.shoot_date_var.set(cfg.get("shoot_date", ""))
                if cfg.get("model") in API_CONFIG:
                    self.model_var.set(cfg["model"])
                if cfg.get("mode") in PROMPT_MODES:
                    self.mode_var.set(cfg["mode"])
```

---

### 修改 F：`_build_iptc_segment` — 添加 Country + Date 参数

**找到函数签名（约 1217 行）：**

```python
def _build_iptc_segment(title, description, keywords, creator=""):
```

**替换为：**

```python
def _build_iptc_segment(title, description, keywords, creator="", country="", date_created=""):
```

**找到函数内 `# 2:80 By-line` 之前（约 1243 行），在它之前插入：**

找到这段：
```python
    # 2:80 By-line
    if creator:
        iptc += iptc_rec(2, 80, creator.encode("utf-8")[:32])
```

**替换为：**

```python
    # 2:55 Date Created — 格式 YYYYMMDD（Getty 自动识别）
    if date_created:
        # 接受 YYYY-MM-DD 或 YYYYMMDD，统一转为 YYYYMMDD
        date_clean = date_created.replace("-", "").replace("/", "")[:8]
        if date_clean.isdigit() and len(date_clean) == 8:
            iptc += iptc_rec(2, 55, date_clean.encode("utf-8"))

    # 2:101 Country/Primary Location Name — Getty 自动填充 Location 字段
    if country:
        iptc += iptc_rec(2, 101, country.strip().encode("utf-8")[:64])

    # 2:80 By-line
    if creator:
        iptc += iptc_rec(2, 80, creator.encode("utf-8")[:32])
```

---

### 修改 G：`_build_xmp_segment` — 添加 Country + Date XMP 字段

**找到函数签名（约 1164 行）：**

```python
def _build_xmp_segment(title, description, keywords, creator=""):
```

**替换为：**

```python
def _build_xmp_segment(title, description, keywords, creator="", country="", date_created=""):
```

**找到 XMP body 内的 `xmlns:xmpRights=` 那一行（约 1175–1176 行），在 `rdf:Description` 标签属性里加入新命名空间，同时在内容里加入新字段。**

找到这段：
```python
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/"
    photoshop:Headline="{t}">
```

**替换为：**

```python
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/"
    xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
    photoshop:Headline="{t}">
```

**找到 `</rdf:Description>` 结束标签之前（约 1203 行），即 `</dc:rights>` 和 `</rdf:Description>` 之间，插入：**

找到这段：
```python
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''
```

**替换为（注意：`country_xmp` 和 `date_xmp` 是局部变量，要在 xmp_body 构建之前计算）：**

先在函数开头（`t, d, cr = ...` 那行之后）插入两行辅助变量：

找到这段：
```python
    t, d, cr = _escape_xml(title), _escape_xml(description), _escape_xml(creator)
```

**替换为：**

```python
    t, d, cr = _escape_xml(title), _escape_xml(description), _escape_xml(creator)
    country_xmp = _escape_xml(country.strip()) if country else ""
    # 将 YYYY-MM-DD 转为 XMP 标准 ISO 格式 YYYY-MM-DD
    date_xmp = ""
    if date_created:
        dc = date_created.replace("/", "-").strip()
        if len(dc) == 8 and dc.isdigit():
            date_xmp = f"{dc[:4]}-{dc[4:6]}-{dc[6:8]}"
        elif len(dc) >= 10:
            date_xmp = dc[:10]
```

然后找到 `</rdf:Description>` 和 `</rdf:RDF>` 之间（约 1203 行）：

```python
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''
```

**替换为：**

```python
{f'   <photoshop:DateCreated>{date_xmp}</photoshop:DateCreated>' if date_xmp else ''}
{f'   <Iptc4xmpCore:CountryName>{country_xmp}</Iptc4xmpCore:CountryName>' if country_xmp else ''}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''
```

---

### 修改 H：`inject_metadata` — 透传新参数

**找到函数签名（约 1268 行）：**

```python
def inject_metadata(jpeg_path, title, description, keywords, creator=""):
```

**替换为：**

```python
def inject_metadata(jpeg_path, title, description, keywords, creator="", country="", date_created=""):
```

**找到函数内两处调用 `_build_xmp_segment` 和 `_build_iptc_segment`（约 1329–1330 行）：**

```python
        xmp_seg = _build_xmp_segment(title, description, keywords, creator)
        iptc_seg = _build_iptc_segment(title, description, keywords, creator)
```

**替换为：**

```python
        xmp_seg = _build_xmp_segment(title, description, keywords, creator, country, date_created)
        iptc_seg = _build_iptc_segment(title, description, keywords, creator, country, date_created)
```

---

### 修改 I：`_save_worker` — 读取 UI 变量并写入 EXIF + 调用新参数

**找到 `_start_save` 方法（约 2203 行）：**

```python
    def _start_save(self, targets, output_dir=None):
        creator = self.creator_var.get().strip()
        self.is_saving = True
        self.btns["💾 Save"].configure(state="disabled")
        self.btns["📁 Save→Folder"].configure(state="disabled")
        self.prog_lbl.configure(text=f"Saving {len(targets)} images...")
        threading.Thread(target=self._save_worker, args=(targets, output_dir, creator), daemon=True).start()
```

**替换为：**

```python
    def _start_save(self, targets, output_dir=None):
        creator = self.creator_var.get().strip()
        country = self.shoot_location_var.get().strip()
        date_created = self.shoot_date_var.get().strip()
        self.is_saving = True
        self.btns["💾 Save"].configure(state="disabled")
        self.btns["📁 Save→Folder"].configure(state="disabled")
        self.prog_lbl.configure(text=f"Saving {len(targets)} images...")
        threading.Thread(
            target=self._save_worker,
            args=(targets, output_dir, creator, country, date_created),
            daemon=True
        ).start()
```

**找到 `_save_worker` 函数签名（约 2211 行）：**

```python
    def _save_worker(self, targets, output_dir=None, creator=""):
```

**替换为：**

```python
    def _save_worker(self, targets, output_dir=None, creator="", country="", date_created=""):
```

**找到 `_save_worker` 内的 piexif exif 字典构建部分（约 2230–2239 行）：**

```python
                try:
                    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
                    exif["0th"][piexif.ImageIFD.ImageDescription] = d.encode("utf-8")
                    if creator:
                        exif["0th"][piexif.ImageIFD.Artist] = creator.encode("utf-8")
                        exif["0th"][piexif.ImageIFD.Copyright] = f"(c) {creator}".encode("utf-8")
                    exif["0th"][0x9C9B] = t.encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9D] = ";".join(k).encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9F] = t.encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9E] = d.encode("utf-16le") + b"\x00\x00"
                    exif["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(d, encoding="unicode")
```

**替换为：**

```python
                try:
                    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
                    exif["0th"][piexif.ImageIFD.ImageDescription] = d.encode("utf-8")
                    if creator:
                        exif["0th"][piexif.ImageIFD.Artist] = creator.encode("utf-8")
                        exif["0th"][piexif.ImageIFD.Copyright] = f"(c) {creator}".encode("utf-8")
                    exif["0th"][0x9C9B] = t.encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9D] = ";".join(k).encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9F] = t.encode("utf-16le") + b"\x00\x00"
                    exif["0th"][0x9C9E] = d.encode("utf-16le") + b"\x00\x00"
                    exif["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(d, encoding="unicode")

                    # ── Getty: 写入 DateTimeOriginal (EXIF 36867) ──
                    if date_created:
                        dc = date_created.replace("/", "-").replace("-", ":").strip()
                        # 接受 YYYY:MM:DD 或 YYYYMMDD，统一转为 YYYY:MM:DD HH:MM:SS
                        if len(dc) == 8 and dc.isdigit():
                            exif_dt = f"{dc[:4]}:{dc[4:6]}:{dc[6:8]} 00:00:00"
                        elif len(dc) >= 10:
                            exif_dt = dc[:10].replace("-", ":") + " 00:00:00"
                        else:
                            exif_dt = None
                        if exif_dt:
                            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_dt.encode("ascii")
                            exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_dt.encode("ascii")
```

**找到 `inject_metadata` 调用处（约 2261 行）：**

```python
                inject_metadata(save_path, t, d, k, creator)
```

**替换为：**

```python
                inject_metadata(save_path, t, d, k, creator, country, date_created)
```

---

## 修改三：Shutterstock CSV 导出（含自动分类）

### 概述

新增一个 `_export_shutterstock_csv` 方法，读取所有已处理卡片的 title/description/keywords，
通过关键词匹配预设分类映射表自动分配 Category 1 / Category 2，输出 Shutterstock 标准 CSV 格式。
在工具栏添加「📊 SS CSV」按钮。

### 修改 J：在文件顶部常量区加入分类映射表

**找到 `KEYWORD_BLACKLIST` 定义之前的空行（约 331 行），在 `RETRY_DELAYS` 和 `API_CONFIG` 之间插入：**

实际上找一个稳定的锚点更安全。找到这段（约 66 行）：

```python
RETRY_DELAYS = [5, 15, 30]  # Exponential backoff: 5s, 15s, 30s
```

**在它之后（下一行）插入：**

```python

# ── Shutterstock 官方分类映射表 ──
# 键：关键词片段（小写，部分匹配）；值：Shutterstock 分类名称
# 按优先级排列：越靠前越优先匹配
SS_CATEGORY_MAP = [
    # 自然 & 户外
    ({"mountain", "forest", "ocean", "river", "lake", "waterfall", "beach", "desert",
      "jungle", "wildlife", "bird", "flower", "plant", "tree", "landscape", "nature",
      "sky", "cloud", "sunset", "sunrise", "fog", "snow", "ice", "autumn", "spring"},
     "Nature"),
    # 旅行 & 地标
    ({"travel", "city", "street", "architecture", "building", "bridge", "monument",
      "cathedral", "temple", "landmark", "tourism", "urban", "skyline", "village",
      "europe", "asia", "africa", "tropical", "destination"},
     "Travel"),
    # 人物 & 生活方式
    ({"woman", "man", "person", "people", "family", "child", "baby", "portrait",
      "lifestyle", "couple", "group", "team", "business person", "model"},
     "People"),
    # 商业 & 科技
    ({"business", "office", "meeting", "technology", "computer", "phone", "digital",
      "data", "finance", "money", "chart", "graph", "startup", "corporate", "ai",
      "robot", "network", "software", "laptop"},
     "Business and Finance"),
    # 食物 & 饮料
    ({"food", "drink", "coffee", "tea", "wine", "beer", "meal", "dish", "fruit",
      "vegetable", "bread", "cake", "dessert", "restaurant", "kitchen", "chef",
      "cooking", "baking", "recipe", "cuisine"},
     "Food and Drink"),
    # 健康 & 医疗
    ({"health", "medical", "doctor", "hospital", "fitness", "yoga", "exercise",
      "sport", "wellness", "gym", "running", "swimming", "meditation", "pill",
      "stethoscope", "surgery", "patient"},
     "Healthcare and Medicine"),
    # 教育 & 科学
    ({"education", "school", "university", "student", "teacher", "book", "library",
      "science", "laboratory", "research", "experiment", "chemistry", "biology",
      "physics", "math", "study", "learn"},
     "Education"),
    # 艺术 & 创意
    ({"art", "painting", "illustration", "design", "creative", "color", "abstract",
      "pattern", "texture", "background", "graphic", "artist", "gallery", "museum",
      "craft", "handmade", "watercolor", "sketch"},
     "Arts and Entertainment"),
    # 建筑 & 室内
    ({"interior", "room", "home", "house", "apartment", "furniture", "decor",
      "living room", "bedroom", "kitchen", "bathroom", "architecture", "ceiling",
      "floor", "wall", "window", "door", "real estate"},
     "Architecture and Buildings"),
    # 假日 & 节庆
    ({"christmas", "halloween", "valentine", "easter", "thanksgiving", "birthday",
      "party", "celebration", "holiday", "festival", "new year", "wedding",
      "anniversary", "gift", "decoration"},
     "Holidays and Celebrations"),
    # 美容 & 时尚
    ({"fashion", "beauty", "makeup", "cosmetics", "skincare", "hair", "clothing",
      "dress", "model", "runway", "luxury", "jewelry", "accessories", "perfume",
      "style", "elegant"},
     "Fashion"),
    # 工业 & 制造
    ({"industrial", "factory", "machine", "manufacturing", "construction", "engineer",
      "worker", "tool", "equipment", "energy", "solar", "wind", "oil", "steel",
      "warehouse", "logistics", "shipping"},
     "Industry"),
    # 默认兜底
    (set(), "Objects and Concepts"),
]


def _infer_ss_categories(title: str, description: str, keywords: list[str]) -> tuple[str, str]:
    """
    根据标题、描述、关键词推断 Shutterstock 的两个分类。
    返回 (category1, category2)。
    """
    combined = " ".join([title, description] + keywords).lower()
    matches = []
    for keyword_set, category in SS_CATEGORY_MAP[:-1]:  # 排除兜底
        if keyword_set and any(kw in combined for kw in keyword_set):
            score = sum(1 for kw in keyword_set if kw in combined)
            matches.append((score, category))
    matches.sort(key=lambda x: -x[0])
    # 取得分最高的两个不同分类
    seen = []
    for _score, cat in matches:
        if cat not in seen:
            seen.append(cat)
        if len(seen) == 2:
            break
    while len(seen) < 2:
        seen.append(SS_CATEGORY_MAP[-1][1])  # 兜底
    return seen[0], seen[1]
```

---

### 修改 K：在 `App` 类添加 `_export_shutterstock_csv` 方法

**找到 `_export` 方法（约 2346 行）：**

```python
    def _export(self):
        if not self.data_cache:
            return
        f = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if f:
            with open(f, "w", encoding="utf-8") as file:
                json.dump([{"file": Path(k).name, **v} for k, v in self.data_cache.items()], file, indent=2, ensure_ascii=False)
            messagebox.showinfo("Done", f"Exported {len(self.data_cache)} items")
```

**在 `_export` 方法之后（它的下一个方法之前）插入：**

```python
    def _export_shutterstock_csv(self):
        """导出 Shutterstock 标准批量上传 CSV（含自动分类）。"""
        import csv

        # 收集所有已有元数据的卡片
        rows = []
        for path, card in self.cards.items():
            t, d, k = card.get_data()
            if not t:
                continue
            cat1, cat2 = _infer_ss_categories(t, d, k)
            rows.append({
                "Filename": Path(path).name,
                "Description": t,
                "Keywords": ", ".join(k),
                "Categories": cat1,
                "Editorial": "",  # 留空（非编辑类）
                "Mature content": "no",
                "Illustration": "no",
            })

        # 也从 data_cache 补充未在卡片里显示的（正常情况下两者一致）
        if not rows:
            for path, data in self.data_cache.items():
                t = str(data.get("title", "")).strip()
                d = str(data.get("description", "")).strip()
                k = data.get("keywords", [])
                if isinstance(k, str):
                    k = [kw.strip() for kw in k.replace(";", ",").split(",") if kw.strip()]
                if not t:
                    continue
                cat1, cat2 = _infer_ss_categories(t, d, k)
                rows.append({
                    "Filename": Path(path).name,
                    "Description": t,
                    "Keywords": ", ".join(k),
                    "Categories": cat1,
                    "Editorial": "",
                    "Mature content": "no",
                    "Illustration": "no",
                })

        if not rows:
            return messagebox.showwarning("Info", "No processed images found.\nRun AI analysis first.")

        save_path = filedialog.asksaveasfilename(
            title="Save Shutterstock CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="shutterstock_upload.csv",
        )
        if not save_path:
            return

        fieldnames = ["Filename", "Description", "Keywords", "Categories", "Editorial", "Mature content", "Illustration"]
        try:
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            result = messagebox.askyesno(
                "Shutterstock CSV Exported",
                f"Exported {len(rows)} rows to:\n{save_path}\n\nOpen file?"
            )
            if result:
                try:
                    os.startfile(save_path)
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror("Export Error", str(e))
```

> **注意：** 字段 `"Categories"` 对应 Shutterstock CSV 模板的第一个分类列。
> Shutterstock 官方模板可能随时更新，使用前建议从后台下载最新模板核对列名。

---

### 修改 L：在工具栏添加「📊 SS CSV」按钮

工具栏按钮通过 `self.btns` 字典管理。找到工具栏按钮配置部分。

**找到 `_setup_workspace` 内定义按钮的代码段。**  
搜索字符串 `"📁 Save→Folder"` 找到按钮定义位置，它附近有一个按钮列表或循环。

在现有工具栏按钮中，找到 `_export` 对应的按钮（通常标签为 `"📤 Export JSON"` 或类似），
在它之后添加 SS CSV 按钮。

**方案：** 搜索工具栏按钮循环，找到构建 `self.btns` 的 `for` 循环。

**找到类似这样的工具栏按钮定义（在 `_setup_workspace` 内）：**

```python
        for label, cmd in [
            ...
            ("📤 Export JSON", self._export),
            ...
        ]:
```

**在 `("📤 Export JSON", self._export),` 这行之后插入：**

```python
            ("📊 SS CSV", self._export_shutterstock_csv),
```

> 如果工具栏不是用循环构建，而是逐个 `ctk.CTkButton` 调用，则在 `_export` 按钮定义后
> 手动追加同样格式的按钮：
> ```python
> ctk.CTkButton(toolbar, text="📊 SS CSV", command=self._export_shutterstock_csv,
>               height=36, fg_color="#7C3AED", hover_color="#6D28D9",
>               font=("Segoe UI", 12), corner_radius=8).pack(side="left", padx=4)
> ```

---

## 执行顺序 & 验证要点

1. **先执行修改 A + B**（缩略图优化），运行程序加载 50 张图，观察是否流畅
2. **执行修改 C–I**（Getty 元数据），保存一张图后用 ExifTool 验证：
   ```
   exiftool -DateTimeOriginal -IPTC:Country-PrimaryLocationName output.jpg
   ```
3. **执行修改 J + K + L**（SS CSV），导出 CSV 后用记事本确认列名正确

---

## 依赖说明

| 功能 | 所需库 | 已在代码中 |
|------|--------|-----------|
| 修改一（缩略图）| `piexif`, `Pillow` | ✅ 已导入 |
| 修改二（Getty）| `piexif` | ✅ 已导入 |
| 修改三（SS CSV）| `csv`（标准库）| ✅ 无需安装 |

无需新增 `pip install`。

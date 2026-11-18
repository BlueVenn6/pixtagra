"""Capture repository screenshots from the real application with real photographs.

No model is called and no user configuration is read: every card is filled with
metadata written for the photograph actually shown, so the images describe what
the interface really displays.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["PIXTAGRA_PACKAGED_TEST_ISOLATION"] = "1"
os.environ["PSS_NO_STARTUP_DIALOG"] = "1"

import pixtagra as m  # noqa: E402


SCENES = [
    ("000000000001.jpg", {
        "title": "Yellow taxi beside a box truck on a wet city street",
        "description": "A yellow taxi waits at a curb while a large box truck passes on a wet street, with a metal fence and a leafy tree in the foreground and city buildings behind.",
        "keywords": ["taxi", "truck", "city street", "wet road", "urban traffic", "yellow cab",
                     "commercial vehicle", "rainy day", "sidewalk", "street scene", "transport",
                     "traffic", "city life", "automobile", "logistics", "delivery", "road",
                     "asphalt", "curb", "fence", "tree", "buildings", "daylight", "outdoors",
                     "vehicle", "driving", "commute", "municipal", "metropolitan", "downtown",
                     "street photography", "documentary", "industrial", "freight", "cargo"],
    }),
    ("000000000019.jpg", {
        "title": "Two brown and white cows resting in a green pasture",
        "description": "Two brown and white cows lie close together in a green grass field under a wide sky, with a distant tree line along the horizon and soft afternoon light across the meadow.",
        "keywords": ["cow", "cattle", "pasture", "green field", "farm animal", "livestock",
                     "brown and white", "grass", "meadow", "rural scene", "agriculture", "farm",
                     "countryside", "grazing", "herd", "dairy", "horizon", "open sky", "daylight",
                     "summer", "outdoors", "nature", "animal", "mammal", "resting", "pair",
                     "tree line", "landscape", "farmland", "domestic animal", "bovine", "field",
                     "pastoral", "tranquil", "natural light"],
    }),
    ("000000000057.jpg", {
        "title": "Tennis player in red shirt serving on a clay court",
        "description": "A young man in a red shirt and patterned shorts prepares a serve on an orange clay tennis court, holding a racket at shoulder height with a ball in the air above him.",
        "keywords": ["tennis", "clay court", "player", "serve", "racket", "tennis ball",
                     "red shirt", "sport", "athlete", "orange court", "sportsman", "action",
                     "competition", "training", "fitness", "summer sport", "outdoors", "game",
                     "athletic", "forehand", "racquet", "sports equipment", "male athlete",
                     "motion", "tennis racket", "court surface", "recreation", "active lifestyle",
                     "sports clothing", "match", "practice", "playing", "tournament", "tennis player",
                     "sunny"],
    }),
    ("000000000063.jpg", {
        "title": "Two people playing a lawn game on green grass",
        "description": "A man swings a wooden bat at a small white ball while another player stands ready nearby on a wide green lawn, with trees and houses visible in the distance.",
        "keywords": ["lawn game", "outdoor activity", "grass", "player", "bat", "white ball",
                     "park", "recreation", "leisure", "two people", "sports", "playing",
                     "green lawn", "summer", "daylight", "outdoors", "yard", "backyard game",
                     "swing", "casual sport", "group", "trees", "open space", "sunny day",
                     "amateur", "fun", "hobby", "active", "family activity", "field", "game",
                     "wooden bat", "grass field", "weekend", "match"],
    }),
]


SCENES_ZH = {
    "000000000001.jpg": {
        "title": "街头停靠的黄色出租车与厢式货车",
        "description": "一辆黄色出租车停靠在湿漉漉的城市街道边，一辆大型厢式货车正从旁驶过。前景有金属栏杆和枝叶繁茂的树木，后方可见城市建筑。",
        "keywords": ["出租车", "货车", "城市街道", "湿滑路面", "城市交通", "黄色出租车",
                     "商用车辆", "雨天", "人行道", "街景", "交通", "运输",
                     "城市生活", "汽车", "物流", "配送", "道路",
                     "柏油路", "路缘", "栏杆", "树木", "建筑", "白天", "户外",
                     "车辆", "行驶", "通勤", "城市道路", "大都市", "市中心",
                     "街头摄影", "工业", "货运", "货物", "阴天"],
    },
    "000000000019.jpg": {
        "title": "绿草地上休憩的两头棕白花牛",
        "description": "两头棕白花色的牛紧挨着卧在绿色的草地中，天空开阔，地平线处可见远处的树线，午后柔和的光线洒在草甸上。",
        "keywords": ["牛", "黄牛", "牧场", "绿地", "农场动物", "牲畜",
                     "棕白花色", "草地", "草甸", "乡村景象", "农业", "农场",
                     "乡村", "放牧", "牛群", "乳牛", "地平线", "开阔天空", "白天",
                     "夏季", "户外", "自然", "动物", "哺乳动物", "休憩", "成对",
                     "树线", "风景", "农田", "家畜", "安静", "田野",
                     "田园", "宁静", "自然光"],
    },
    "000000000057.jpg": {
        "title": "红衫网球运动员在红土场上发球",
        "description": "一名穿红色上衣和花纹短裤的年轻男子在橙色红土球场上准备发球，球拍举至肩高，球在他上方悬于空中。",
        "keywords": ["网球", "红土球场", "运动员", "发球", "球拍", "网球",
                     "红色上衣", "体育", "运动员", "橙色球场", "男子运动员", "动作",
                     "比赛", "训练", "健身", "夏季运动", "户外", "竞技",
                     "运动", "正手", "球拍运动", "运动器材", "男性运动员",
                     "动感", "网球场", "场地", "休闲运动", "积极生活", "运动服",
                     "对抗", "练习", "击球", "锦标赛", "网球选手", "晴天"],
    },
    "000000000063.jpg": {
        "title": "两人在草地上进行户外球类游戏",
        "description": "一名男子挥动木质球棒击打白色小球，另一名玩家在旁准备接应，广阔的绿色草坪延伸至远处，可见树木与房屋。",
        "keywords": ["草地游戏", "户外活动", "草坪", "玩家", "球棒", "白球",
                     "公园", "休闲", "娱乐", "两人", "运动", "游玩",
                     "绿草地", "夏季", "白天", "户外", "院子", "庭院游戏",
                     "挥击", "休闲运动", "群体", "树木", "开阔空间", "晴天",
                     "业余", "趣味", "爱好", "活跃", "家庭活动", "场地", "游戏",
                     "木质球棒", "草地", "周末", "对局"],
    },
}


def main(output_dir):
    from PIL import ImageGrab
    import ctypes

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    corpus = Path(m.__file__).resolve().parent / ".build-rc" / "stress-corpus"

    scratch = tempfile.mkdtemp(prefix="pixtagra-shots-")
    m.APP_CONFIG_DIR = Path(scratch)
    m.CONFIG_FILE = Path(scratch) / "config.json"
    m.VAULT_DIR = Path(scratch) / "unused-vault"
    m.VAULT_FILE = m.VAULT_DIR / "keys.vault"
    m.SALT_FILE = m.VAULT_DIR / "keys.salt"

    sources = []
    for name, _data in SCENES:
        path = corpus / name
        if not path.exists():
            raise SystemExit(f"missing corpus photo: {path}")
        sources.append(str(path))

    m.filedialog.askopenfilenames = lambda **kw: sources
    errors = []
    app = m.App()
    app.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
    app.title("PixTagra")
    app.geometry("1680x1000+0+0")
    app.deiconify()

    def pump(seconds=0.15):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            app.update()
            time.sleep(0.01)

    pump(0.4)
    app.load_images()
    for _ in range(200):
        pump(0.05)
        if not app.is_importing:
            break
    pump(0.5)

    for path, (name, data) in zip(sources, SCENES):
        card = app.cards.get(path)
        if card is None:
            raise SystemExit(f"card not created: {name}")
        payload = dict(data)
        payload["compliance"] = {}
        payload["metadata_language"] = "en"
        payload["keyword_language"] = "en"
        payload["keyword_policy_version"] = 3
        card._cached_data = {}
        card.var_sel.set(False)
        card.fill(payload)
    pump(0.4)
    app._update_stats()
    pump(0.4)

    def capture(label, language):
        app._on_language_change(language)
        pump(0.7)
        app.update_idletasks()
        pump(0.3)
        hwnd = ctypes.windll.user32.GetParent(app.winfo_id())
        target = output / f"pixtagra-{label}.png"
        ImageGrab.grab(window=hwnd).save(target)
        print(f"wrote {target}")

    capture("english", "English")

    # The Chinese screenshot shows Chinese metadata, so the record language
    # matches the interface language the reader sees.
    for path, (name, _data) in zip(sources, SCENES):
        card = app.cards.get(path)
        if card is None:
            continue
        payload = dict(SCENES_ZH[name])
        payload["compliance"] = {}
        payload["metadata_language"] = "zh"
        payload["keyword_language"] = "zh"
        payload["keyword_policy_version"] = 3
        card.var_sel.set(False)
        card.fill(payload)
    pump(0.4)
    app._update_stats()
    pump(0.4)

    capture("chinese", "简体中文")

    app.destroy()
    if errors:
        print("callback errors:", errors, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

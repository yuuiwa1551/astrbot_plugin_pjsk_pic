from __future__ import annotations

from typing import Any

CHARACTERS = (
    ("初音未来", "Hatsune Miku"), ("镜音铃", "Kagamine Rin"), ("镜音连", "Kagamine Len"), ("巡音流歌", "Megurine Luka"), ("MEIKO", "MEIKO"), ("KAITO", "KAITO"),
    ("星乃一歌", "Ichika Hoshino"), ("天马咲希", "Saki Tenma"), ("望月穗波", "Honami Mochizuki"), ("日野森志步", "Shiho Hinomori"), ("花里实乃理", "Minori Hanasato"), ("桐谷遥", "Haruka Kiritani"), ("桃井爱莉", "Airi Momoi"), ("日野森雫", "Shizuku Hinomori"),
    ("小豆泽心羽", "Kohane Azusawa"), ("白石杏", "An Shiraishi"), ("东云彰人", "Akito Shinonome"), ("青柳冬弥", "Toya Aoyagi"), ("天马司", "Tsukasa Tenma"), ("凤笑梦", "Emu Otori"), ("草薙宁宁", "Nene Kusanagi"), ("神代类", "Rui Kamishiro"), ("宵崎奏", "Kanade Yoisaki"), ("朝比奈真冬", "Mafuyu Asahina"), ("东云绘名", "Ena Shinonome"), ("晓山瑞希", "Mizuki Akiyama"),
)
GROUPS = (
    ("VIRTUAL SINGER", ("初音未来", "镜音铃", "镜音连", "巡音流歌", "MEIKO", "KAITO")), ("Leo/need", ("星乃一歌", "天马咲希", "望月穗波", "日野森志步")), ("MORE MORE JUMP!", ("花里实乃理", "桐谷遥", "桃井爱莉", "日野森雫")), ("Vivid BAD SQUAD", ("小豆泽心羽", "白石杏", "东云彰人", "青柳冬弥")), ("Wonderlands×Showtime", ("天马司", "凤笑梦", "草薙宁宁", "神代类")), ("25时、Nightcord见。", ("宵崎奏", "朝比奈真冬", "东云绘名", "晓山瑞希")),
)

JAPANESE_NAMES = (
    '初音ミク', '鏡音リン', '鏡音レン', '巡音ルカ', 'MEIKO', 'KAITO',
    '星乃一歌', '天馬咲希', '望月穂波', '日野森志歩',
    '花里みのり', '桐谷遥', '桃井愛莉', '日野森雫',
    '小豆沢こはね', '白石杏', '東雲彰人', '青柳冬弥',
    '天馬司', '鳳えむ', '草薙寧々', '神代類',
    '宵崎奏', '朝比奈まふゆ', '東雲絵名', '暁山瑞希',
)
KNOWN_PAIRINGS = {'杏豆': ('白石杏', '小豆泽心羽'), '遥实': ('桐谷遥', '花里实乃理')}

class ChatImageCollectionService:
    def __init__(self, db):
        self.db = db
        self._candidates: list[dict[str, Any]] | None = None

    def _initialize_candidates(self):
        ids = {}
        result = []
        for (name, alias), japanese in zip(CHARACTERS, JAPANESE_NAMES):
            matches = [self.db.resolve_tag(value, allow_fuzzy=False) for value in (name, japanese, alias)]
            existing_matches = {match.tag_id: match for match in matches if match.matched}
            existing = existing_matches[min(existing_matches)] if existing_matches else None
            canonical = existing.tag_name if existing else name
            duplicates = [match.tag_name for match in existing_matches.values() if match.tag_name != canonical]
            if duplicates:
                self.db.merge_tags(canonical, duplicates)
            ids[name] = self.db.get_or_create_tag(canonical, tag_type='character')
            for value in (name, japanese, alias):
                if value != canonical:
                    self.db.add_alias(canonical, value)
            result.append({'tag_id': ids[name], 'name': canonical, 'standard_name': name,
                           'name_en': alias, 'name_ja': japanese, 'tag_type': 'character'})
        for name, members in GROUPS:
            result.append({'tag_id': self.db.get_or_create_tag(name, tag_type='theme'), 'name': name, 'tag_type': 'theme', 'member_ids': [ids[x] for x in members]})
        for alias in ('25时', '25時', '25時、ナイトコードで。'):
            self.db.add_alias('25时、Nightcord见。', alias)
        for name in KNOWN_PAIRINGS:
            row = self.db.get_tag_row(name)
            if row is not None and row['tag_type'] != 'pairing':
                self.db.set_tag_type(name, 'pairing')
        for row in self.db.list_tags(keyword='', limit=200, character_only=None):
            if str(row['status'] or 'active') == 'active' and str(row['tag_type'] or '') == 'pairing':
                candidate = {'tag_id': int(row['id']), 'name': str(row['name']), 'tag_type': 'pairing'}
                if row['name'] in KNOWN_PAIRINGS:
                    candidate['member_ids'] = [ids[x] for x in KNOWN_PAIRINGS[row['name']]]
                result.append(candidate)
        return result

    def ensure_candidates(self):
        if self._candidates is None:
            self._candidates = self._initialize_candidates()
        return [dict(item) for item in self._candidates]

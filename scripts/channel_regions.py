#!/usr/bin/env python3
"""Shared output-group rules for the living-room IPTV playlists.

Many public Chinese playlists use a catch-all ``中文综合`` group even when a
station name clearly identifies Hong Kong, Taiwan, Japan, or a mainland
province/city.  Keep source grouping separate from the final APTV grouping and
derive the latter from station identity in one shared, idempotent place.
"""

from __future__ import annotations

import re


MAINLAND_REGION_GROUPS = (
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南",
    "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆",
)

OUTPUT_GROUP_ORDER = (
    "卫视台",
    *MAINLAND_REGION_GROUPS,
    "其他地方",
    "中文付费",
    "香港", "澳门", "台湾", "新加坡", "马来西亚", "日本",
    "娱乐", "体育", "少儿", "音乐", "教育", "财经",
)

GENERIC_MAINLAND_GROUPS = {"大陆", "中文综合", "地方台", "其他地方"}


def _pattern(*tokens: str) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(token) for token in tokens), re.I)


# Check non-mainland regions before the generic ``卫视`` rule.  Otherwise
# 凤凰卫视、香港卫视 and 澳门莲花卫视 would be mixed into mainland satellites.
NON_MAINLAND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "香港",
        re.compile(
            r"(?:\btvb\b|\brthk\b|\bviutv\b|\bhoy\b|香港卫视|凤凰(?:卫视)?(?:中文|资讯|香港)|"
            r"鳳凰(?:衛視)?(?:中文|資訊|香港)|翡翠台|无线(?:新闻)?|無線(?:新聞)?|"
            r"港台电视|港台電視|\bnow\s*(?:新闻|新聞|财经|財經))",
            re.I,
        ),
    ),
    (
        "澳门",
        re.compile(r"(?:澳门|澳門|澳视|澳視|澳亚|澳亞|莲花卫视|蓮花衛視|\btdm\b)", re.I),
    ),
    (
        "台湾",
        re.compile(
            r"(?:台湾|台灣|(?<![a-z])tvbs(?![a-z])|台视|台視|中视|中視|华视|華視|民视|民視|"
            r"公视|公視|三立|东森|東森|中天|年代|非凡|镜新闻|鏡新聞|寰宇|"
            r"靖天|靖洋|龙华|龍華|八大|大爱|大愛|momo|国会频道|國會頻道|"
            r"运通财经|運通財經|信大|靖天|靖洋|博斯|纬来|緯來)",
            re.I,
        ),
    ),
    (
        "新加坡",
        re.compile(r"(?:新加坡|\bchannel\s*(?:8|u)\b|8\s*[频道頻道]|u\s*[频道頻道])", re.I),
    ),
    (
        "马来西亚",
        re.compile(r"(?:马来西亚|馬來西亞|\bastro\b|八度空间|八度空間)", re.I),
    ),
    (
        "日本",
        re.compile(
            r"(?:[\u3040-\u30ff]|\b(?:nhk|tokyo\s*mx|tbs|fuji\s*tv|tv\s*asahi|"
            r"nippon\s*tv|ytv|mbs|abc\s*tv|bs\s*(?:asahi|fuji|tbs|ntv))\b)",
            re.I,
        ),
    ),
)


# Province names plus prefecture/city labels commonly present in local IPTV
# station names.  Specific province rules intentionally precede vague words
# such as 都市、公共、民生, which remain in ``其他地方`` when no location exists.
MAINLAND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("北京", _pattern("北京", "BRTV", "卡酷")),
    ("上海", _pattern("上海", "上视", "第一财经", "东方影视", "七彩戏剧", "法治天地", "游戏风云", "欢笑剧场")),
    ("天津", _pattern("天津")),
    ("重庆", _pattern("重庆", "重慶")),
    ("河北", _pattern("河北", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水", "平泉", "清河", "任丘", "昌黎", "兴隆")),
    ("山西", _pattern("山西", "太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁", "太谷", "平遥")),
    ("内蒙古", _pattern("内蒙古", "内蒙", "呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安盟", "锡林郭勒", "阿拉善")),
    ("辽宁", _pattern("辽宁", "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛")),
    ("吉林", _pattern("吉林", "长春", "四平", "辽源", "通化", "白山", "松原", "白城", "延边")),
    ("黑龙江", _pattern("黑龙江", "哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河", "绥化", "大兴安岭")),
    ("江苏", _pattern("江苏", "南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城", "扬州", "镇江", "泰州", "宿迁", "优漫")),
    ("浙江", _pattern("浙江", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水", "钱江", "之江", "海宁")),
    ("安徽", _pattern("安徽", "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州", "六安", "亳州", "池州", "宣城")),
    ("福建", _pattern("福建", "福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德")),
    ("江西", _pattern("江西", "南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶")),
    ("山东", _pattern("山东", "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照", "临沂", "德州", "聊城", "滨州", "菏泽", "齐鲁")),
    ("河南", _pattern("河南", "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源")),
    ("湖北", _pattern("湖北", "武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州", "恩施", "仙桃", "潜江", "天门", "神农架")),
    ("湖南", _pattern("湖南", "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化", "娄底", "湘西")),
    ("广东", _pattern("广东", "广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门", "茂名", "惠州", "梅州", "汕尾", "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮", "大湾区")),
    ("广西", _pattern("广西", "南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池", "来宾", "崇左")),
    ("海南", _pattern("海南", "海口", "三亚", "三沙", "儋州", "琼海", "文昌", "万宁", "东方", "五指山")),
    ("四川", _pattern("四川", "成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山")),
    ("贵州", _pattern("贵州", "贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔西南", "黔东南", "黔南")),
    ("云南", _pattern("云南", "昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "楚雄", "红河", "文山", "西双版纳", "大理", "德宏", "怒江", "迪庆", "易门", "通海")),
    ("西藏", _pattern("西藏", "拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里")),
    ("陕西", _pattern("陕西", "西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛")),
    ("甘肃", _pattern("甘肃", "兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南", "临夏", "甘南")),
    ("青海", _pattern("青海", "西宁", "海东", "海北", "黄南", "海南州", "果洛", "玉树", "海西")),
    ("宁夏", _pattern("宁夏", "银川", "石嘴山", "吴忠", "固原", "中卫")),
    ("新疆", _pattern("新疆", "乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "昌吉", "博尔塔拉", "巴音郭楞", "阿克苏", "克孜勒苏", "喀什", "和田", "伊犁", "塔城", "阿勒泰", "兵团")),
)


def regionalized_group(identity: str, current_group: str) -> str:
    """Return the user-facing region for a station identity.

    Explicit upstream region/category groups are authoritative.  Inference is
    applied only to mainland catch-all groups and already-classified mainland
    regions, which makes repeated publication passes safe and idempotent.
    """
    group = (current_group or "").strip()
    if group == "卫视台":
        return group
    if group not in GENERIC_MAINLAND_GROUPS and group not in MAINLAND_REGION_GROUPS:
        return group

    for output_group, pattern in NON_MAINLAND_RULES:
        if pattern.search(identity):
            return output_group

    if re.search(r"(?:\bcctv[\s_-]*(?:\d{1,2}|4k)|央视)", identity, re.I):
        return "卫视台"
    if re.search(r"卫视(?:台)?(?:\s|$)|衛視(?:台)?(?:\s|$)", identity, re.I):
        return "卫视台"

    for output_group, pattern in MAINLAND_RULES:
        if pattern.search(identity):
            return output_group
    return group if group in MAINLAND_REGION_GROUPS else "其他地方"


def group_sort_index(group: str) -> int:
    try:
        return OUTPUT_GROUP_ORDER.index(group)
    except ValueError:
        return len(OUTPUT_GROUP_ORDER) + 1

"""微信扫描器配置"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# ===== 微信数据路径 =====
WECHAT_DATA_ROOT = Path(os.getenv(
    "WECHAT_DATA_ROOT",
    os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    ),
))

# 解密后的数据库目录（wechat-decrypt 工具输出）
DECRYPTED_DB_DIR = Path(os.getenv(
    "DECRYPTED_DB_DIR",
    os.path.expanduser("~/Desktop/feishu/wechat-decrypt/decrypted"),
))

# ===== 复用飞书 bot 的配置 =====
# 飞书应用凭证（bridge 用来创建 lark.Client 扫群聊）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# bot 项目路径（bridge 用来 import bot 的 services）
FEISHU_DEAL_BOT_DIR = os.getenv(
    "FEISHU_DEAL_BOT_DIR",
    os.path.expanduser("~/Desktop/feishu/feishu-deal-bot"),
)

# 多维表格（与 feishu-deal-bot 共享同一个表）
BITABLE_APP_TOKEN = os.getenv("BITABLE_APP_TOKEN", "")
BITABLE_TABLE_ID = os.getenv("BITABLE_TABLE_ID", "")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Claude CLI（分析用）
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "")

# 员工名单
STAFF_NAMES = [
    n.strip() for n in
    os.getenv("STAFF_NAMES", "Alex C.,Alvin萧,Angela张溶溶,Clara江志桐,Cody周天奇,Cynthia张倩,董鹏,杜薇,Hanqin张翰钦,黄凯,李美尚,鲁园,邵海涛,唐安颖,TongTong陈彤彤,Wang Chuan,王涵,Xavier张煜程,肖迪馨,徐瑞阳,Yan朱颜,杨珂,Yvonne,Zeno张鑫,钟献彬,周凯,邹笑辉").split(",")
    if n.strip()
]

# lark-cli 路径（写入 bitable 用）
LARK_CLI_PATH = os.getenv(
    "LARK_CLI_PATH",
    os.path.expanduser("~/.npm-global/lib/node_modules/@larksuite/cli/bin/lark-cli"),
)

# ===== 被投组合名单 =====
# hardcoded: AI 据此区分 "新项目" vs "被投跟踪"
# 格式: "公司名 (英文/中文均可)"
# TODO: 用户提供完整名单后替换
PORTFOLIO_COMPANIES = [
    # 1-30: 天际资本 (人民币基金)
    "昂瑞微", "思特威", "好达", "统信", "果纳", "芯来",
    "酷芯微电子", "鹍远", "龙旗", "飞泽复材", "开云", "海飞科",
    "纽劢科技", "派恩杰", "华天软件", "赛丽科技", "视涯", "创米",
    "华存", "芯盟", "云之深", "尊湃", "星云", "聆思", "新声",
    "微容", "开源", "亿麦矽", "赛丽", "未来智能",
    # 31-60: 天际资本 (续)
    "恺望数据", "Dify", "可触未来", "爱梦恩", "子牛亦东", "RWKV",
    "比特智路", "ideaflow", "诺视", "龙焱", "格物", "大为机器人",
    "青心意创", "来牟", "熙华", "蔚能", "卫蓝新能源",
    "南京安立格有限公司", "光创联", "电科星拓", "埃芯半导体",
    "速易联芯", "数字光芯", "南麟", "字节跳动", "金山云",
    "小米科技", "蔚来汽车", "美团点评", "国双科技",
    # 61-90: 天际资本 + NYX Ventures
    "老虎证券", "好大夫在线", "伴鱼", "PingCap", "康沣", "健世",
    "黑芝麻", "云智慧", "壹点灵", "路特斯", "大湾生物",
    "tetramem", "PlatoX", "Pixocial", "fastmoss", "LiveX",
    "Mistral", "Strella", "Euka", "The Forecasting",
    "Pastel Health", "Olive legal", "Alaris", "Educato",
    "Gyges仙瞬", "Fastmoss", "Nuva Lab", "Virtual Factory",
    "OpenFunnel", "Redsky", "Aris Fund",
    # 91-111: NYX Ventures
    "OrbitalNerds", "Cyber Partner", "Dexmate", "Befreed", "Wearlinq",
    "Chance AI", "Corgi", "Caseflood AI", "Mimoto", "OSport AI",
    "Paradigm Study", "Toro Bio", "Foresight health", "BrandPal",
    "Traini", "Artlas", "Boogoo", "AiPai", "World Engine",
    "Classie", "Arda",
    # 补充
    "Simular", "Fellou", "Dinq", "Sophon AI",
]

# ===== 扫描设置 =====
# 区域扫描：每次抓取的消息数量
CONTEXT_WINDOW_SIZE = int(os.getenv("CONTEXT_WINDOW_SIZE", "30"))

# 全局扫描：批次大小（每次分析多少条消息）
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))

# 是否跳过 Agent2（验证 Agent），改用代码验证
# True（默认）: 用代码做 portfolio matching + confidence scoring，省掉一次 Claude 调用
# False: 保留原有双 Agent 架构
SKIP_VALIDATION_AGENT = os.getenv("SKIP_VALIDATION_AGENT", "true").lower() in ("true", "1", "yes")

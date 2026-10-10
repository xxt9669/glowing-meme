#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京东商品降价监控脚本 (monitor.py)
================================
 
功能：
  1. 从 products.txt 读取京东商品链接
  2. 定时查询每个商品的当前价格（京东官方价格接口）
  3. 价格存入 SQLite 数据库，保留完整历史记录
  4. 与上次价格对比，降价时通过 Server酱 发微信通知
  5. SendKey 从环境变量读取，不写死在代码里
 
用法：
  首次测试（只跑一次）：  python monitor.py --once
  持续监控（循环运行）：  python monitor.py
  自定义间隔（秒）：      python monitor.py --interval 1800
  测试通知是否能发：      python monitor.py --test-notify
"""
 
import os
import re
import sys
import time
import sqlite3
import requests
from datetime import datetime
 
# ---- 可选：自动加载 .env 文件 ----
# 如果安装了 python-dotenv，会自动读取当前目录下的 .env 文件
# 没装也没关系，会直接从系统环境变量读取
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
 
# ================================================================
#  配置区（根据自己的需要修改这些参数）
# ================================================================
 
# 商品列表文件名（每行一个京东商品链接）
PRODUCTS_FILE = "products.txt"
 
# 数据库文件名（SQLite，自动生成）
DB_FILE = "jd_monitor.db"
 
# 检查间隔（秒）。3600 = 1小时，1800 = 半小时
CHECK_INTERVAL = 3600
 
# 京东价格接口（不用改）
# 调用时会把 { } 替换成商品ID
JD_PRICE_API = "https://p.3.cn/prices/mgets?skuIds=J_{}"
 
# Server酱 接口（不用改）
# 调用时会把 { } 替换成你的 SendKey
SERVERCHAN_API = "https://sctapi.ftqq.com/{}.send"
 
# 请求头（模拟浏览器访问，防止被京东屏蔽）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
 
 
# ================================================================
#  工具函数
# ================================================================
 
def get_sendkey():
    """
    从环境变量读取 Server酱 的 SendKey
 
    环境变量名：SC_SENDKEY
    你可以在 .env 文件里写 SC_SENDKEY=你的key
    也可以在命令行执行 export SC_SENDKEY=你的key
 
    返回：SendKey 字符串，如果没配置返回 None
    """
    # os.environ.get() 从环境变量读取，找不到就返回默认值 ""
    key = os.environ.get("SC_SENDKEY", "").strip()
 
    if not key:
        print("\n⚠️  没有配置 SendKey！降价时无法发通知。")
        print("   请设置环境变量 SC_SENDKEY，方法：")
        print("   1. 创建 .env 文件，写入：SC_SENDKEY=你的key")
        print("   2. 或者命令行执行：export SC_SENDKEY=你的key")
        return None
 
    return key
 
 
def load_products():
    """
    从 products.txt 读取商品列表
    
    文件格式（每行一个商品）：
      方式1 - 直接写链接：https://item.jd.com/100012043978.html
      方式2 - 名称和链接用竖线分隔：我的手机|https://item.jd.com/100012043978.html
 
    以 # 开头的行是注释，会被忽略
    空行也会被忽略
 
    返回：列表，每项是一个元组 (商品名称, 商品链接)
          如果用户没写名称，商品名称就是空字符串 ""
    """
    # 检查文件是否存在
    if not os.path.exists(PRODUCTS_FILE):
        print(f"❌ 找不到商品列表文件：{PRODUCTS_FILE}")
        print(f"   请先创建该文件，每行写一个京东商品链接")
        return []
 
    products = []
 
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()           # 去掉首尾空格和换行符
            if not line:                 # 空行跳过
                continue
            if line.startswith("#"):      # 注释行跳过
                continue
 
            # 检查是否有竖线分隔的名称
            if "|" in line:
                # split("|", 1) 表示只按第一个竖线分割
                name, url = line.split("|", 1)
                products.append((name.strip(), url.strip()))
            else:
                # 没有名称，只存链接
                products.append(("", line))
 
    print(f"📦 已加载 {len(products)} 个商品")
    return products
 
 
def extract_sku_id(url):
    """
    从京东商品链接中提取商品ID（也叫 SKU ID）
 
    例如：
      https://item.jd.com/100012043978.html  →  "100012043978"
      100012043978                            →  "100012043978"
 
    参数：url - 京东商品链接或纯数字ID
    返回：商品ID字符串，提取失败返回 None
    """
    # 方法1：用正则从完整链接中提取数字ID
    # 正则含义：匹配 item.jd.com/ 后面的连续数字
    match = re.search(r'item\.jd\.com/(\d+)', url)
    if match:
        return match.group(1)   # group(1) 返回括号里匹配到的内容
 
    # 方法2：如果用户直接填了纯数字，也接受
    if re.fullmatch(r'\d+', url):
        return url
 
    # 都不匹配，说明链接格式不对
    return None
 
 
def get_display_name(name, sku_id):
    """
    获取商品的显示名称
    如果用户在 products.txt 里写了名称，就用那个
    如果没写，就用 "商品{sku_id}" 代替
    """
    if name:
        return name
    return f"商品{sku_id}"
 
 
def get_jd_price(sku_id):
    """
    调用京东价格接口，获取商品当前价格
 
    接口地址：https://p.3.cn/prices/mgets?skuIds=J_{商品ID}
    返回的 JSON 类似：
      [{"id":"J_100012043978", "p":"999.00", "m":"1299.00", "op":"999.00"}]
    其中 "p" 就是当前价格
 
    参数：sku_id - 商品ID
    返回：价格（浮点数），获取失败返回 None
    """
    # 拼接完整的接口地址
    url = JD_PRICE_API.format(sku_id)
 
    try:
        # 发送 GET 请求，超时10秒
        resp = requests.get(url, headers=HEADERS, timeout=10)
        # 设置编码为 utf-8
        resp.encoding = "utf-8"
        # 把返回的 JSON 字符串解析成 Python 字典/列表
        data = resp.json()
 
        # 检查返回的数据是否正常
        # data 是一个列表，第一个元素是包含价格的字典
        if data and len(data) > 0 and "p" in data[0]:
            # data[0]["p"] 是价格字符串，如 "999.00"
            # float() 把字符串转成浮点数
            price = float(data[0]["p"])
            return price
        else:
            print(f"   ⚠️  商品 {sku_id}：接口未返回价格数据")
            return None
 
    except requests.RequestException as e:
        # 网络请求出错（断网、超时等）
        print(f"   ⚠️  商品 {sku_id}：网络请求失败 - {e}")
        return None
    except (ValueError, KeyError, IndexError) as e:
        # JSON 解析出错或字段不存在
        print(f"   ⚠️  商品 {sku_id}：数据解析失败 - {e}")
        return None
 
 
# ================================================================
#  数据库操作（SQLite）
# ================================================================
 
def init_db():
    """
    初始化 SQLite 数据库
 
    创建一张表叫 price_history，用来记录每次查到的价格
    字段说明：
      id         - 自增主键（每条记录自动编号）
      sku_id     - 商品ID
      url        - 商品链接
      price      - 价格
      checked_at - 检查时间（文本格式，如 "2024-01-15 14:30:00"）
 
    如果表已经存在，不会重复创建（IF NOT EXISTS）
    """
    # 连接数据库文件（不存在会自动创建）
    conn = sqlite3.connect(DB_FILE)
 
    # 执行建表语句
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_id      TEXT    NOT NULL,
            url         TEXT,
            price       REAL,
            checked_at  TEXT    NOT NULL
        )
    """)
 
    # 提交更改并关闭连接
    conn.commit()
    conn.close()
    print(f"💾 数据库就绪：{DB_FILE}")

 
def save_price(sku_id, url, price):
    """
    把一次价格查询结果存入数据库
 
    参数：
      sku_id - 商品ID
      url    - 商品链接
      price  - 当前价格
    """
    conn = sqlite3.connect(DB_FILE)
 
    # 获取当前时间，格式如 "2024-01-15 14:30:00"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
 
    # 插入一条记录
    # ? 是占位符，后面的元组按顺序填入
    conn.execute(
        "INSERT INTO price_history (sku_id, url, price, checked_at) VALUES (?, ?, ?, ?)",
        (sku_id, url, price, now)
    )
 
    conn.commit()
    conn.close()
 
 
def get_last_price(sku_id):
    """
    查询某商品上一次记录的价格
 
    从数据库中找到该商品最新一条记录的 price 值
    （注意：在 save_price 之前调用，所以拿到的是上一次的价格）
 
    参数：sku_id - 商品ID
    返回：价格（浮点数），没有历史记录返回 None
    """
    conn = sqlite3.connect(DB_FILE)
 
    # 查询语句：找到 sku_id 匹配的记录，按 id 倒序排列，取第一条
    # ORDER BY id DESC 意思是按 id 从大到小排（最新的在最前面）
    # LIMIT 1 意思是只取第一条
    cur = conn.execute(
        "SELECT price FROM price_history WHERE sku_id = ? ORDER BY id DESC LIMIT 1",
        (sku_id,)
    )
 
    # fetchone() 取出查询结果的第一行
    row = cur.fetchone()
    conn.close()
 
    # 如果有结果，row 是一个元组如 (999.0,)，取第一个元素 row[0]
    # 如果没有结果，row 是 None
    if row:
        return row[0]
    return None
 
 
# ================================================================
#  Server酱 通知
# ================================================================
 
def send_notify(sendkey, title, content):
    """
    通过 Server酱 发送微信通知
 
    参数：
      sendkey - 你的 SendKey
      title   - 通知标题（简短）
      content - 通知内容（支持 Markdown 格式）
 
    返回：True=成功，False=失败
    """
    try:
        # 向 Server酱 接口发送 POST 请求
        # text 是标题，desp 是正文（支持 Markdown）
        resp = requests.post(
            SERVERCHAN_API.format(sendkey),
            data={"text": title, "desp": content},
            headers=HEADERS,
            timeout=10
        )
        # 解析返回的 JSON
        result = resp.json()
 
        # Server酱 返回 code=0 表示发送成功
        if result.get("code") == 0:
            print("   ✅ 通知已发送到微信")
            return True
        else:
            print(f"   ❌ 通知发送失败：{result.get('message', result)}")
            return False
 
    except Exception as e:
        print(f"   ❌ 通知发送异常：{e}")
        return False
# ================================================================
#  核心逻辑：执行一次价格检查
# ================================================================
 
def check_once():
    """
    执行一次完整的价格检查流程：
 
    1. 加载商品列表
    2. 逐个查询价格
    3. 与上次价格对比
    4. 降价则记录下来
    5. 所有商品查完后，统一发一条通知
    6. 把本次价格存入数据库
    """
    print(f"\n{'='*55}")
    print(f"🕐 开始检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")
 
    # ---- 第1步：加载商品列表 ----
    products = load_products()
    if not products:
        print("没有商品可检查，请先在 products.txt 中添加商品链接")
        return
 
    # ---- 第2步：读取 SendKey（可能为空，空的话只记录不发通知）----
    sendkey = get_sendkey()
 
    # 用来收集所有降价的商品
    dropped_items = []
 
    # ---- 第3步：逐个查询价格 ----
    for name, url in products:
        # 从链接中提取商品ID
        sku_id = extract_sku_id(url)
        if not sku_id:
            print(f"\n⚠️  无法提取商品ID：{url}")
            continue
 
        display_name = get_display_name(name, sku_id)
        print(f"\n🔍 检查：{display_name}")
        print(f"   链接：{url}")
 
        # 获取当前价格
        current_price = get_jd_price(sku_id)
        if current_price is None:
            print(f"   跳过（未获取到价格）")
            continue
 
        print(f"   当前价格：¥{current_price}")
 
        # 获取上次记录的价格（注意：此时还没存入本次价格）
        last_price = get_last_price(sku_id)
 
        if last_price is None:
            # 首次记录，没有历史数据可以对比
            print(f"   📝 首次记录，已存入数据库")
 
        elif current_price < last_price:
            # 降价了！
            drop_amount = last_price - current_price         # 降了多少
            drop_pct = (drop_amount / last_price) * 100      # 降了百分之几
            print(f"   📉 降价！上次 ¥{last_price} → 现在 ¥{current_price}")
            print(f"       降了 ¥{drop_amount:.2f}（{drop_pct:.1f}%）")
 
            # 把降价信息收集起来，等所有商品查完一起发通知
            dropped_items.append({
                "name": display_name,
                "sku_id": sku_id,
                "url": url,
                "last_price": last_price,
                "current_price": current_price,
                "drop_amount": drop_amount,
                "drop_pct": drop_pct,
            })
 
        elif current_price > last_price:
            # 涨价了（不发通知，只打印）
            print(f"   📈 涨价了：上次 ¥{last_price} → 现在 ¥{current_price}（不发通知）")
 
        else:
            # 价格没变
            print(f"   ➖ 价格不变：¥{current_price}")
 
        # 把本次价格存入数据库
        save_price(sku_id, url, current_price)
 
        # 每个商品之间间隔2秒，避免请求太快被京东屏蔽
        time.sleep(2)
# ---- 第4步：如果有降价商品，发微信通知 ----
    if dropped_items:
        print(f"\n{'='*55}")
        print(f"📉 共 {len(dropped_items)} 个商品降价！")
 
        if sendkey:
            # 组装通知内容（Markdown 格式，Server酱 支持 Markdown）
            content = "## 京东商品降价提醒\n\n"
            content += f"共检测到 **{len(dropped_items)}** 个商品降价：\n\n"
 
            for i, item in enumerate(dropped_items, 1):
                content += f"### {i}. {item['name']}\n"
                content += f"- 链接：[点击查看]({item['url']})\n"
                content += f"- 上次价格：¥{item['last_price']}\n"
                content += f"- 当前价格：¥{item['current_price']}\n"
                content += f"- 降价：¥{item['drop_amount']:.2f}（{item['drop_pct']:.1f}%）\n\n"
 
            content += "---\n*由京东降价监控程序自动发送*"
 
            # 发送通知
            send_notify(sendkey, "京东商品降价提醒", content)
        else:
            print("⚠️  有商品降价，但未配置 SendKey，无法发送微信通知")
 
    else:
        print(f"\n✅ 本次检查完毕，没有商品降价")
 
    print(f"🕐 检查结束 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
 
 
# ================================================================
#  测试通知
# ================================================================
 
def test_notify():
    """
    发送一条测试通知，验证 SendKey 是否配置正确
 
    用法：python monitor.py --test-notify
    """
    print("📧 正在发送测试通知...")
    sendkey = get_sendkey()
    if not sendkey:
        return
 
    title = "京东监控 - 测试通知"
    content = (
        "这是一条测试通知。\n\n"
        "如果你在微信上收到了这条消息，说明 SendKey 配置正确！\n\n"
        "监控程序已就绪 ✅\n\n"
        "---\n*来自京东降价监控程序*"
    )
 
    send_notify(sendkey, title, content)
 
 
# ================================================================
#  主函数（程序入口）
# ================================================================
 
def main():
    """
    主函数：解析命令行参数，启动监控
 
    支持的参数：
      --once           只检查一次（适合测试）
      --test-notify    发送一条测试通知（验证 SendKey）
      --interval=N     设置检查间隔为 N 秒（默认 3600 = 1小时）
    """
    # 默认参数
    run_once = False         # 是否只跑一次
    interval = CHECK_INTERVAL  # 检查间隔
    test_mode = False        # 是否只测通知
 
    # 遍历命令行参数
    for arg in sys.argv[1:]:
        if arg == "--once":
            run_once = True
        elif arg == "--test-notify":
            test_mode = True
        elif arg.startswith("--interval="):
            # 提取等号后面的数字
            try:
                interval = int(arg.split("=")[1])
            except ValueError:
                print(f"❌ --interval 需要一个数字，你输入的是：{arg}")
                sys.exit(1)
        else:
            print(f"❌ 不认识的参数：{arg}")
            print(f"   支持的参数：--once, --test-notify, --interval=秒数")
            sys.exit(1)
 
    # 初始化数据库
    init_db()
 
    # 测试通知模式
    if test_mode:
        test_notify()
        return
 
    # 单次检查模式（适合第一次测试）
    if run_once:
        check_once()
        return
# 持续监控模式（默认）
    print(f"\n🚀 启动持续监控模式，每 {interval} 秒（约 {interval/60:.0f} 分钟）检查一次")
    print(f"   按 Ctrl+C 可以停止")
    print(f"   首次检查即将开始...")
 
    # 无限循环，不断检查
    while True:
        try:
            check_once()
            print(f"\n⏰ 下次检查在 {interval} 秒后...")
            time.sleep(interval)
 
        except KeyboardInterrupt:
            # 用户按了 Ctrl+C，退出程序
            print("\n\n👋 已停止监控，再见！")
            break
 
        except Exception as e:
            # 出现意外错误，等一会儿再试
            print(f"\n❌ 发生错误：{e}")
            print(f"   {interval} 秒后自动重试...")
            time.sleep(interval)
 
 
# 程序入口：当直接运行这个文件时，执行 main 函数
if __name__ == "__main__":
    main()

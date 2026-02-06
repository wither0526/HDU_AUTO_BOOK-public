import requests
import yaml
import random
from datetime import datetime, timedelta
import json
import os
import logging

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import time


# 配置日志输出格式
logging.basicConfig(
    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    level=logging.DEBUG
)

time_zone = 8  # 设置时区为北京时间 (UTC+8)

def get_seats_with_config(user_config, date_config, seat_config):
    """
    根据配置获取可用的座位列表。

    参数:
        user_config (dict): 用户配置信息，包含每日的预约偏好。
        date_config (dict): 特定日期的预约配置（如周一、周二等）。
        seat_config (dict): 座位区域的详细配置（如座位号范围、类型）。

    返回:
        list: 可用的座位号列表。
    """
    # 获取座位区域名称（如：二楼东、守正书院等）
    seat_name = date_config['name']
    
    # 如果是自定义座位，直接返回用户配置中的座位列表
    if seat_name == "自定义":
        return user_config['自定义']
    
    # 否则根据区域配置生成座位号范围列表
    # range(start, end) 生成从 start 到 end-1 的整数序列
    return list(range(seat_config[seat_name]['begin'], seat_config[seat_name]['end']))


class SeatAutoBooker:
    """
    自动预约座位的主要类。
    包含初始化驱动、登录、获取用户信息、预约座位等功能。
    """

    def __init__(self, booker_config):
        """
        初始化 SeatAutoBooker 实例。

        参数:
            booker_config (dict): 预约器的基础配置信息。
        """
        self.json = None
        self.resp = None
        self.user_data = None

        logging.info('正在创建 SeatAutoBooker 对象...')

        # 从环境变量获取学号和密码
        self.un = os.environ["SCHOOL_ID"].strip()  # 学号
        print("使用用户：{}".format(self.un))
        self.pd = os.environ["PASSWORD"].strip()  # 密码
        
        self.SCKey = None
        try:
            self.SCKey = os.environ["SCKEY"]
        except KeyError:
            print("没有Server酱的key,将不会推送消息")

        # 配置 Chrome 浏览器选项
        chrome_options = Options()
        # 在 GitHub Actions 等无头环境中必须使用 headless 模式
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--remote-debugging-port=9222')
        chrome_options.add_argument('--window-size=1920,1080')
        
        # 页面加载策略：eager (DOMContentLoad 触发即算加载完成，不等待资源)
        chrome_options.page_load_strategy = 'eager'

        # 修复沙箱权限问题：设置自定义用户数据目录
        # 防止在受限环境（如 Trae Sandbox）中因无法访问系统临时目录而崩溃
        import os
        project_root = os.path.dirname(os.path.abspath(__file__))
        user_data_dir = os.path.join(project_root, ".chrome_data")
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir)
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
        
        # 尝试多种方式加载 chromedriver
        try:
            # 方式1：尝试自动管理驱动 (Selenium 4.x+)
            self.driver = webdriver.Chrome(options=chrome_options)
            logging.info('使用自动下载/管理的 ChromeDriver')
        except Exception as e1:
            logging.warning(f'自动初始化 ChromeDriver 失败: {e1}')
            try:
                # 方式2：尝试 Linux 常用路径（适用于 GitHub Actions Linux 环境）
                self.driver = webdriver.Chrome(service=Service('/usr/local/bin/chromedriver'), options=chrome_options)
                logging.info('使用 Linux 路径的 ChromeDriver')
            except Exception as e2:
                logging.warning(f'Linux 路径 ChromeDriver 失败: {e2}')
                try:
                    # 方式3：尝试从系统 PATH 环境变量中查找
                    self.driver = webdriver.Chrome(options=chrome_options)
                    logging.info('使用 PATH 中的 ChromeDriver')
                except Exception as e3:
                    logging.error(f'无法加载 ChromeDriver: {e3}')
                    raise

        # 设置显式等待，超时时间 10 秒
        self.wait = WebDriverWait(self.driver, 10, 0.5)
        self.cookie = None

        self.cfg = booker_config

    def book_favorite_seat(self, user_config, seat_config):
        """
        根据配置执行预约操作。
        主要逻辑：判断当前时间是否在预约窗口内，如果在则尝试预约。

        参数:
            user_config (dict): 用户配置。
            seat_config (dict): 座位配置。
        
        返回:
            tuple: (状态码, 消息)
        """
        # 判断预约目标日期：后天 (The day after tomorrow)
        # weekday(): 0=周一, 6=周日
        # (today + 2) % 7 计算后天的星期几
        the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
        
        # 获取后天配置的座位类型（自习室 or 生活区）
        seat_type = seat_config[user_config[the_day_after_tomorrow]['name']]["type"]
        
        # 根据座位类型确定预约开放时间
        # 注意：这里的时间计算似乎是基于 UTC 时间进行的调整
        # time_zone = 8
        # hour = 20 - 8 = 12 (UTC 12:00 = Beijing 20:00)
        if seat_type == "自习室":
            # 自习室：20:00 开放预约
            start_time = datetime.now().replace(hour=20-time_zone, minute=0, second=0, microsecond=0)
            end_time = datetime.now().replace(hour=20-time_zone, minute=15, second=0, microsecond=0)
        else:
            # 其他（生活区）：21:00 开放预约 (根据原有代码逻辑)
            start_time = datetime.now().replace(hour=21-time_zone, minute=0, second=0, microsecond=0)
            end_time = datetime.now().replace(hour=21-time_zone, minute=15, second=0, microsecond=0)
            
        # 允许提前 cron-delta-minutes 分钟开始运行脚本进行等待
        start_time = start_time - timedelta(minutes=self.cfg["cron-delta-minutes"])
        
        # 检查当前时间是否在允许的运行窗口内
        if datetime.now() < start_time or datetime.now() > end_time:
            return -1, "未到预约时间"
            
        logging.info('开始执行预约流程...')
        
        # 计算重试间隔时间
        # 逻辑：(提前时间 * 2) / (最大重试次数 - 2) - 10秒缓冲
        retry_sleep_time = timedelta(minutes=self.cfg["cron-delta-minutes"]).seconds*2/(self.cfg["max-retry"]-2) - 10
        
        # 循环尝试预约
        for tried_times in range(self.cfg["max-retry"]):
            try:
                return self._book_favorite_seat(user_config, seat_config, tried_times)
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "尝试第{}次失败".format(tried_times))
                time.sleep(retry_sleep_time)

    def _book_favorite_seat(self, user_config, seat_config, tried_times=0):
        """
        执行具体的预约 HTTP 请求。

        参数:
            user_config (dict): 用户配置。
            seat_config (dict): 座位配置。
            tried_times (int): 当前已尝试的次数。

        返回:
            tuple: (API返回码, 结果消息)
        """
        logging.info('进入 _book_favorite_seat 方法')
        
        # 确定目标日期（后天）
        the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
        date_config = user_config[the_day_after_tomorrow]
        
        # 获取可用座位列表
        seats = get_seats_with_config(user_config, date_config, seat_config)
        
        # 计算预约的开始时间戳
        today_0_clock = datetime.strptime(datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S")
        # 目标时间 = 今天0点 + 2天 + 开始小时数
        book_time = today_0_clock + timedelta(days=2) + timedelta(hours=date_config['开始时间'])
        
        # 计算距离基准时间的差值（用于生成 API 所需的时间戳）
        # 修复：确保 self.cfg["start-time"] 是 datetime 对象
        config_start_time = self.cfg["start-time"]
        if isinstance(config_start_time, str):
            # 假设格式为 YYYY-MM-DD HH:MM:SS，这里简单处理，具体视 basic_config 而定
            # 如果 basic_config 中是日期字符串，需要解析
             try:
                config_start_time = datetime.strptime(config_start_time, "%Y-%m-%d %H:%M:%S")
             except ValueError:
                # 兼容只写日期的情况，默认为 08:00:00 (根据之前经验)
                config_start_time = datetime.strptime(config_start_time, "%Y-%m-%d").replace(hour=8)
        
        # 计算时间差
        delta = book_time - config_start_time
        # 转换为总秒数
        total_seconds = delta.days * 24 * 3600 + delta.seconds
        
        # 选择座位
        # 如果是自定义座位且尝试次数较少，优先尝试第一个；否则随机选择
        if date_config['name'] == '自定义' and tried_times < self.cfg["max-retry"]/3*2:
            seat = seats[0]
        else:
            seat = random.choice(seats)
            
        # 构造 POST 请求数据
        # beginTime: 距离基准时间的秒数
        # duration: 持续时间（秒）
        # seats[0]: 座位号
        # seatBookers[0]: 用户 UID
        data = f"beginTime={total_seconds}&duration={3600 * date_config['持续小时数']}&&seats[0]={seat}&seatBookers[0]={self.user_data['uid']}"

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        print(f"发送预约请求: {data}")
        
        # 发送请求
        self.resp = requests.post(self.cfg["target"], data=data, headers=headers)
        self.json = json.loads(self.resp.text)
        
        return self.json["CODE"], self.json["MESSAGE"] + " 座位:{}".format(seat)

    def login(self):
        """
        执行登录流程。
        使用 Selenium 模拟浏览器登录 SSO 系统并获取 Cookie。

        返回:
            int: 0 表示成功，-1 表示失败。
        """
        logging.info('开始登录流程')

        try:
            logging.info('正在跳转到登录页面...')

            # SSO 登录 URL
            sso_url = "https://sso.hdu.edu.cn/login?service=https:%2F%2Fhdu.huitu.zhishulib.com%2FUser%2FIndex%2FhduCASLogin%3Fforward%3D%252FSpace%252FCategory%252Fredirect%253Fcategory_id%253D591"
            self.driver.get(sso_url)
            logging.info('打开 SSO 登录页面: ' + sso_url)

            # 等待页面元素加载
            self.wait.until(EC.presence_of_element_located((By.NAME, "username")))
            logging.debug('找到用户名输入框.')
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
            logging.debug('找到密码输入框.')
            self.wait.until(EC.presence_of_element_located((By.CLASS_NAME, "login-button")))
            logging.debug('找到登录按钮.')

            # 输入用户名
            username_input = self.driver.find_element(By.NAME, 'username')
            username_input.clear()
            username_input.send_keys(self.un)
            logging.info('已输入用户名')

            # 输入密码
            password_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
            password_input.clear()
            password_input.send_keys(self.pd)
            logging.info('已输入密码')

            # 点击登录
            logging.info('点击登录按钮')
            login_button = self.driver.find_element(By.CLASS_NAME, "login-button")
            login_button.click()
            
            # 等待重定向完成
            time.sleep(5)
            logging.info('登录完成，当前URL: {}'.format(self.driver.current_url))

            # 获取并保存 Cookie
            cookie_list = self.driver.get_cookies()
            self.cookie = ";".join([item["name"] + "=" + item["value"] + "" for item in cookie_list])
            self.cfg["headers"]['Cookie'] = self.cookie
            logging.info('获取 Cookie 成功，数量: {}'.format(len(cookie_list)))

            logging.info("登录成功！")
        except Exception as e:
            logging.error(f"登录失败：{e}")
            # 登录失败截图
            try:
                self.driver.save_screenshot("login_error.png")
                logging.info("已保存错误截图为 login_error.png")
            except:
                pass
            return -1
        return 0

    def get_user_info(self):
        """
        获取用户详细信息（如 UID）。
        
        返回:
            int: 0 表示成功，-1 表示失败。
        """
        logging.info('正在获取用户信息...')

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        try:
            # 请求用户座位信息接口
            resp = requests.get("https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1",
                                headers=headers)
            self.user_data = resp.json()['DATA']
            # 验证是否包含 uid
            _ = self.user_data['uid']
        except Exception as e:
            logging.exception(e)
            print(self.user_data)
            print(e.__class__.__name__ + ",获取用户数据失败")
            return -1
        print("获取用户数据成功")
        return 0

    def wechatNotice(self, message, desp=None):
        """
        发送 Server酱 微信推送通知。

        参数:
            message (str): 消息标题。
            desp (str, optional): 消息详情。
        """
        logging.info('正在发送微信通知...')

        if self.SCKey and self.SCKey != '':
            url = 'https://sctapi.ftqq.com/{0}.send'.format(self.SCKey)
            data = {
                'title': message,
                desp: desp,
            }
            try:
                r = requests.post(url, data=data)
                if r.json()["data"]["error"] == 'SUCCESS':
                    print("Server酱通知成功")
                else:
                    print("Server酱通知失败")
            except Exception as e:
                logging.exception(e)
                print(e.__class__, "推送服务配置错误")


def is_booking_enable(date_cfg):
    """
    检查指定日期的预约功能是否启用。

    参数:
        date_cfg (dict): 日期配置信息。

    返回:
        bool: True 表示启用，False 表示禁用。
    """
    if date_cfg['启用']:
        return True
    return False


if __name__ == "__main__":
    logging.info('程序启动')
    
    # 加载配置文件
    with open("user_config.yml", 'r', encoding='utf-8') as f_obj:
        user_config = yaml.safe_load(f_obj)
    with open("config/basic_config.yml", 'r', encoding='utf-8') as f_obj:
        basic_config = yaml.safe_load(f_obj)
    with open("config/seat_config.yml", 'r', encoding='utf-8') as f_obj:
        seat_config = yaml.safe_load(f_obj)

    # 检查后天的预约是否启用
    the_day_after_tomorrow = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(datetime.now().weekday() + 2) % 7]
    if not is_booking_enable(user_config[the_day_after_tomorrow]):
        logging.info('预约未启用')
        print("预约未启用")
        exit(0)

    # 初始化预约器
    s = SeatAutoBooker(basic_config["SeatAutoBooker"])
    
    # 登录
    if not s.login() == 0:
        s.driver.quit()
        logging.info('登录失败，程序退出')
        exit(-1)
        
    # 获取用户信息
    if not s.get_user_info() == 0:
        s.driver.quit()
        logging.info('获取用户信息失败，程序退出')
        exit(-1)
        
    # 执行预约
    s.book_favorite_seat(user_config=user_config, seat_config=seat_config)
    
    # 退出驱动
    s.driver.quit()
    logging.info('程序结束')

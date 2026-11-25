"""团团通配置。全部外部值走环境变量，见仓库根 .env.example。"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env()
environ.Env.read_env(REPO_ROOT / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

WXPUSHER_SUBSCRIBE_TENANT_SCHEMA = env("WXPUSHER_SUBSCRIBE_TENANT_SCHEMA", default="")
WXPUSHER_SUBSCRIBE_URL = env("WXPUSHER_SUBSCRIBE_URL", default="")
WXPUSHER_SUBSCRIBE_QR_URL = env("WXPUSHER_SUBSCRIBE_QR_URL", default="")
WXPUSHER_APP_TOKEN = env("WXPUSHER_APP_TOKEN", default="")
WXPUSHER_APP_ID = env("WXPUSHER_APP_ID", default="").strip()
WXPUSHER_LOGIN_SIGNING_KEY = env("WXPUSHER_LOGIN_SIGNING_KEY", default="")
JIDANPU_CALLBACK_HOSTS = env.json("JIDANPU_CALLBACK_HOSTS", default={})

WECOM_ARCHIVE_TENANT_SCHEMA = env("WECOM_ARCHIVE_TENANT_SCHEMA", default="kexing")
WECOM_ARCHIVE_PROGRAM_ID = env("WECOM_ARCHIVE_PROGRAM_ID", default="")
WECOM_ARCHIVE_PRIVATE_KEY_DIR = env("WECOM_ARCHIVE_PRIVATE_KEY_DIR", default="")
WECOM_ARCHIVE_RETENTION_DAYS = env.int("WECOM_ARCHIVE_RETENTION_DAYS", default=90)
WECOM_ARCHIVE_VIEWERS = env.json("WECOM_ARCHIVE_VIEWERS", default={})
WECOM_ZONE_ENABLED = env.bool("WECOM_ZONE_ENABLED", default=False)
WECOM_ZONE_LOG_TARGETS = env.list("WECOM_ZONE_LOG_TARGETS", default=[])
WECOM_REFUND_NOTICE_RULE_ID = env("WECOM_REFUND_NOTICE_RULE_ID", default="").strip()
WECOM_REFUND_NOTICE_CHAT_ID = env("WECOM_REFUND_NOTICE_CHAT_ID", default="").strip()
WECOM_REFUND_NOTICE_START_AT = env("WECOM_REFUND_NOTICE_START_AT", default="").strip()

# public schema 的应用
SHARED_APPS = [
    "django_tenants",
    "apps.platform",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # 鸡蛋铺回调接收器：无模型、不进 `config/urls.py`，HTTP 入口是 `127.0.0.1:8002` 上的独立 WSGI app
    # `apps.callback.wsgi`（systemd 单元起，不走按 Host 认租户）。进 SHARED_APPS 只为让
    # `manage.py callback_events` 能被发现；⛔ 不放 TENANT_APPS——回调没有租户归属，属平台面。
    "apps.callback",
]

# 每个租户 schema 各有一份的应用
TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "apps.business",
    "apps.accounts",
    "apps.rbac",
    "apps.audit",
    # 开放平台（对外 API 的凭据与授权范围）。⛔ **只在 TENANT_APPS**：每个租户 schema 各一份，
    # 与 `apps.business` 同口径（spec `docs/specs/2026-10-07-open-platform-api-design.md`
    # §4-D1 / §5.1：表里不带 `tenant_id` 列）。
    "apps.openapi",
    "apps.integration_sync",
    # 打手侧开放接口（凭据 + 绑定打手账号）。⛔ 只在 TENANT_APPS：表承载租户业务数据，每个租户
    # schema 各一份、不带 `tenant_id` 列（spec `docs/specs/2026-10-08-taker-open-api-design.md` §3）。
    # ⚠️ 与 `apps.openapi` 是两条独立通道（打手令牌结构上打不开上家侧端点），见 `apps/takerapi/models.py` 的 `TakerApp` docstring ①。
    "apps.takerapi",
    # 实名认证（租户侧：核验流水 + 每租户一行的开关配置）。⛔ 只在 TENANT_APPS：表承载租户业务数据
    # （谁核过、结果是什么），每个租户 schema 各一份。平台侧那两张（密钥包 / 拨给租户的额度）在
    # `apps.platform`（`SHARED_APPS`，public 平面，spec §4.1）——⛔ 别混。
    "apps.realname",
    # QQ 机器人（群推送 / 入群审批）。⛔ **只在 TENANT_APPS**：
    # 全部数据（机器人凭据、群清单、推送配置、待发行、审批留痕）都是**租户业务数据**，
    # 与 `apps.business` 同口径 ⇒ 每个租户 schema 各一份，表里不带 `tenant_id` 列。
    "apps.qqbot",
    # 微信公众号自开发集成（D-0268：租户配置单行 + 用户绑定行）。⛔ 只在 TENANT_APPS：表承载租户业务
    # 数据（租户自己的公众号凭据、谁绑了 openid），每个租户 schema 各一份。
    # ⚠️ 迁移是独立序列（`0001_initial`），不进 business 序列（计划「落位与迁移·H1」）。
    "apps.wechatmp",
    # 企微自建应用（D-0379：租户配置单行 + 回调消息台账）。⛔ 只在 TENANT_APPS：表承载租户业务数据
    # （租户自己的企微凭据、回调收到的消息），每个租户 schema 各一份、不带 `tenant_id` 列。
    # ⚠️ 迁移是独立序列（`0001_initial`），不进 business / rbac 序列。
    "apps.wecom",
    "apps.settlement_control",
    "apps.internal_chat",
]

INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "platform.Tenant"
TENANT_DOMAIN_MODEL = "platform.Domain"

# ── 每租户独立 database（B 档隔离，D-0137）──
# 库名与角色名一律 `{TENANT_DB_PREFIX}{schema_name}` 派生（清单里不存，存了必漂移）。
TENANT_DB_PREFIX = env("TENANT_DB_PREFIX", default="tt_")
TT_SHARED_WEB_ROUTES_FILE = env("TT_SHARED_WEB_ROUTES_FILE", default="")
TT_PLATFORM_EGRESS_ROUTES_FILE = env("TT_PLATFORM_EGRESS_ROUTES_FILE", default="")
TT_ORDER_FINANCE_BROWSER_FILE = env("TT_ORDER_FINANCE_BROWSER_FILE", default="")
TT_ORDER_FINANCE_REUSE_SCHEMAS = frozenset(env.list("TT_ORDER_FINANCE_REUSE_SCHEMAS", default=[]))
TT_ORDER_FINANCE_RUNTIME_DIR = env("TT_ORDER_FINANCE_RUNTIME_DIR", default="")
TT_ORDER_FINANCE_GLOBAL_HEAVY_READS = env.int("TT_ORDER_FINANCE_GLOBAL_HEAVY_READS", default=8)
TT_ORDER_FINANCE_TENANT_HEAVY_READS = env.int("TT_ORDER_FINANCE_TENANT_HEAVY_READS", default=4)
TT_ORDER_FINANCE_DIAGNOSTICS_ENABLED = env.bool("TT_ORDER_FINANCE_DIAGNOSTICS_ENABLED", default=True)

# 供给用的运维身份。⛔ **实际要求是超管**（非超管方案未实现未测，见 docs/RUNBOOK.md §2.5.1）：
# `CREATEDB + CREATEROLE` **不够**（跑不了已实现的供给 SQL）。
# 回退到 DATABASES 里的应用身份（本地/CI 无此角色时仍能起服务，但供给会明确报错）。
if TT_SHARED_WEB_ROUTES_FILE:
    POSTGRES_ADMIN_USER = ""
    POSTGRES_ADMIN_PASSWORD = ""
    DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
else:
    POSTGRES_ADMIN_USER = env("POSTGRES_ADMIN_USER", default=env("POSTGRES_USER"))
    POSTGRES_ADMIN_PASSWORD = env("POSTGRES_ADMIN_PASSWORD", default=env("POSTGRES_PASSWORD"))
    DATABASES = {
        "default": {
            "ENGINE": "django_tenants.postgresql_backend",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER"),
            "PASSWORD": env("POSTGRES_PASSWORD"),
            "HOST": env("POSTGRES_HOST", default="127.0.0.1"),
            "PORT": env("POSTGRES_PORT", default="5432"),
            # 连接存活秒数。默认 0 有意：本机连本地库、重连开销可忽略；生产（托管实例）由机器 `.env` 给 60——
            # 真机实测应用机→托管库 TCP 建连约 2.7 ms + SCRAM 往返，不开则每请求重连、每页白付几十毫秒；
            # 连接数上限实测 2048、当时占用 14 ⇒ 2 个 gunicorn worker 各持一条毫无压力。
            "CONN_MAX_AGE": env.int("POSTGRES_CONN_MAX_AGE", default=0),
            # 与 CONN_MAX_AGE 成对：持连接必遇"服务端掐空闲连接"，这个开关让 Django 在请求开始时先验活
            # （失效则换一条），避免 `server closed the connection unexpectedly` 这类看起来像代码 bug 的 500——
            # 单独开长连接不验活，等于把必然发生的运维现象伪装成偶发代码缺陷。
            "CONN_HEALTH_CHECKS": env.bool("POSTGRES_CONN_HEALTH_CHECKS", default=True),
        }
    }
POSTGRES_ADMIN_DB = env("POSTGRES_ADMIN_DB", default="postgres")
# 车队清单（运维库存；**不是**权威租户目录）。不进 git，仓库只提交 fleet.example.json。
TENANT_FLEET_FILE = env("TENANT_FLEET_FILE", default=str(REPO_ROOT / "fleet.json"))

DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)

MIDDLEWARE = [
    (
        "config.shared_web.SharedTenantMiddleware"
        if TT_SHARED_WEB_ROUTES_FILE
        else "django_tenants.middleware.main.TenantMainMiddleware"
    ),
    # 请求体墙（E-0095）：把非直传路径恢复到 Django 出厂默认的 2.5 MiB（见下面
    # `DATA_UPLOAD_MAX_MEMORY_SIZE` 的说明）。排在租户解析之后（未知域名仍由租户层给 404）、
    # Session/CSRF/认证之前（超大请求不落会话与 DB 工作）；完整口径见 `apps/core/middleware.py` 类 docstring。
    "apps.core.middleware.NonDirectUploadBodyLimitMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 排在 AuthenticationMiddleware 之后：它需要 request.user 已求值，
    # 也要覆盖"哈希逐字节相同、Django 兜不住"的情形（spec §4.3）。
    "apps.core.middleware.TenantSessionGuardMiddleware",
    "apps.core.order_finance_browser.OrderFinanceBrowserMiddleware",
    "apps.core.middleware.AndroidAppVersionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # 请求耗时埋点（spec docs/plans/2026-10-04-request-metrics.md §5）。排最后=最靠近视图：
    # 只有排这里它的 get_response() 才包含租户解析/会话/CSRF/认证这些外层耗时（要全链路时间）。
    # ⚠️ 外层中间件短路（如 401、未知域名 404）时它不会被调用 ⇒ 那些请求不记录；开关见 `apps/core/request_metrics.py`。
    "apps.core.request_metrics.RequestMetricsMiddleware",
]

# 主仓只有租户面一个平面；平台控制台已迁独立项目 `../ttt-platform`（D-0196，主仓那套已删），无 env 开关。
# ⛔ 不要改用 `PUBLIC_SCHEMA_URLCONF`：它是全局设置，会让每台租户机在 public 平面也长出 `/admin/`（spec §2.2-C3）。
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TT_CHAT_ORDER_GROUPS_ENABLED = env.bool("TT_CHAT_ORDER_GROUPS_ENABLED", default=False)
TT_INTERNAL_CHAT_VISITORS_ENABLED = env.bool("TT_INTERNAL_CHAT_VISITORS_ENABLED", default=False)
TT_CHAT_REALTIME_SECRET = env("TT_CHAT_REALTIME_SECRET", default="")
TT_CHAT_REALTIME_TENANT_KEYS = env.bool("TT_CHAT_REALTIME_TENANT_KEYS", default=False)
TT_CHAT_REALTIME_NOTIFY_URL = env("TT_CHAT_REALTIME_NOTIFY_URL", default="")
TT_CHAT_SERVICE_CONFIG_FILE = env("TT_CHAT_SERVICE_CONFIG_FILE", default="")
TT_CHAT_READ_BACKEND = env("TT_CHAT_READ_BACKEND", default="legacy")
TT_CHAT_CORE_BACKEND = env("TT_CHAT_CORE_BACKEND", default="legacy")
TT_CHAT_MAINTENANCE = env.bool("TT_CHAT_MAINTENANCE", default=False)
APP_ANDROID_MIN_VERSION_CODE = env.int("APP_ANDROID_MIN_VERSION_CODE", default=0)

# 静态文件。`django.contrib.staticfiles` 已安装，而 DRF 默认渲染器含
# BrowsableAPIRenderer，其模板调用 {% static %}——缺 STATIC_URL 会直接抛
# ImproperlyConfigured，第一个在浏览器打开的 APIView 就是 500。
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "direct_assignment_console": {"class": "logging.StreamHandler"},
        "hall_pull_console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "apps.business.jidanpu_direct_taker": {
            "handlers": ["direct_assignment_console"], "level": "INFO", "propagate": False,
        },
        "apps.business.api.peer_views.hall_pull_trace": {
            "handlers": ["hall_pull_console"], "level": "INFO", "propagate": False,
        },
    },
}

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "apps.core.exceptions.envelope_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # ⚠️ 顺序是契约：Session 在前、Bearer 在后（实测后定）。DRF 用第一个认证类的
        # authenticate_header() 填 401 的 WWW-Authenticate——Bearer 排前会把 Web 端匿名 401 的头
        # 从 Session 变成 Bearer（`test_p5_envelope_csrf` 钉死）。Session 在前对 App 无损失（App 不带会话 cookie）。
        "apps.core.authentication.TenantBoundAuthentication",
        # 打手 App 线：`Authorization: Bearer <token>`，与上面的 Session 并存。
        "apps.core.authentication.AppTokenAuthentication",
    ],
    # fail-closed：漏声明 permission_classes 的接口默认要求认证（spec D11）。DRF 出厂默认是 AllowAny
    # （rest_framework/settings.py），漏写即静默公开 ⇒ 公开接口必须显式声明 AllowAny。刻意推迟到 P2 合并后开启：
    # P2 端点彼时按"无认证"实现，提前开会让匿名测试集体变红、失败难归因（开启时机见 P5 spec §13）。
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

# ── 打手押金闸（D-0269）：True ⇒ 抢单前必须冻结该游戏要求的押金（不够从余额补，再不够就拒单）。
# ⚠️ 默认 False，与实名闸同一先例：改变「谁能接单」的新闸门，上线=逐租户显式打开（`DEPOSIT_ENFORCED=1` 进该租户 `.env` 并重启）；
# 只拦 App 时用下面的 `DEPOSIT_ENFORCED_APP`（⛔ 别顺手开总闸）。症状：漏开=「押金规则配了但接单不拦」；开错=「网页端打手也接不了单」。
DEPOSIT_ENFORCED = env.bool("DEPOSIT_ENFORCED", default=False)

# 只对打手 App（Bearer 令牌）生效的押金闸，与总闸分开控制：`DEPOSIT_ENFORCED`=网页+App 一起拦，
# 本开关=只拦 App 令牌、网页端会话不受影响（业务方要求分开）。判据在
# `apps/business/deposit_gate.py::deposit_enforced_for`，⛔ 别在视图里各写一份。
DEPOSIT_ENFORCED_APP = env.bool("DEPOSIT_ENFORCED_APP", default=False)

# ── 打手端在线客服（Chatwoot 网页小部件，spec `docs/specs/2026-10-21-taker-app-customer-service.md` §2）：承载页 URL 由后端拼
# （`apps/takerapi/api/chat_views.py`，`h`=HMAC-SHA256(identifier, 收件箱 hmac 密钥)），密钥绝不能进客户端 ⇒ 只住本文件/.env；
# 四个键任一为空 ⇒ 端点回「未开通」（200+enabled=false，⛔ 不 500）；`WEBSITE_TOKEN` 公开、不进 URL，只当"配没配齐"的闸。
CHATWOOT_BASE_URL = env("CHATWOOT_BASE_URL", default="")
CHATWOOT_WEBSITE_TOKEN = env("CHATWOOT_WEBSITE_TOKEN", default="")
#: 收件箱的 hmac 密钥（**私密**）：只用于算 `identifier_hash`，⛔ 不回给客户端、⛔ 不进日志。
CHATWOOT_HMAC_TOKEN = env("CHATWOOT_HMAC_TOKEN", default="")
#: 承载页路径。默认 `/app-chat.html`（spec §3 那份源文件的发版落点）。
CHATWOOT_PAGE_PATH = env("CHATWOOT_PAGE_PATH", default="/app-chat.html")

# ── 网页端 Chatwoot 小部件（收件箱与 App 那条线分开；计划 `docs/plans/2026-10-22-web-chatwoot-widget.md` §2.3）。
# ⛔ 不要与 App 的 `CHATWOOT_*` 混用（两个收件箱 hmac 密钥不同，共用=改一处静默串箱）。三个键都非空时
# `/api/auth/{me,login}` 的 `user` 载荷才加 `chat_identifier(_hash)`；任一为空 ⇒ 前端 fail-closed 不挂气泡（⛔ 不退化成匿名会话）。
CHATWOOT_WEB_BASE_URL = env("CHATWOOT_WEB_BASE_URL", default="")
CHATWOOT_WEB_WEBSITE_TOKEN = env("CHATWOOT_WEB_WEBSITE_TOKEN", default="")
#: 网页收件箱的 hmac 密钥（**私密**）：只用于算 `identifier_hash`。
CHATWOOT_WEB_HMAC_TOKEN = env("CHATWOOT_WEB_HMAC_TOKEN", default="")

# ── 短信（打手 App 线）：`console`=验证码打进日志（开发/测试）；`tencent`=腾讯云 SendSms。
# ⚠️ 生产必须设 `SMS_BACKEND=tencent`，否则打手收不到验证码但接口看起来一切正常；
# 密钥只走 env（`.env` 不进 git），本模块不把它们写进任何响应或日志。
SMS_BACKEND = env("SMS_BACKEND", default="console")
SMS_TENCENT = {
    "secret_id": env("SMS_TENCENT_SECRET_ID", default=""),
    # 腾讯云控制台「云 API 密钥」里的 SecretKey（不是短信应用的 App Key）。
    "secret_key": env("SMS_TENCENT_SECRET_KEY", default=""),
    "sdk_app_id": env("SMS_TENCENT_SDK_APP_ID", default=""),
    # 签名**内容**（如「代代通」），⛔ 不是签名 ID（685372 那种）。
    "sign_name": env("SMS_TENCENT_SIGN_NAME", default=""),
    "template_id": env("SMS_TENCENT_TEMPLATE_ID", default=""),
    "region": env("SMS_TENCENT_REGION", default="ap-guangzhou"),
}

# ── Jeepay 支付网关（充值，D-0269 P2）──────────────────────────────
# 业务方已有实例：网关 `https://pay.suqing.chat`（端口见其中转配置）。
# 商户号 / 应用 appId / 签名密钥在 Jeepay 商户后台（mch.suqing.chat）里查。
JEEPAY_GATEWAY = env("JEEPAY_GATEWAY", default="https://pay.suqing.chat")
JEEPAY_MCH_NO = env("JEEPAY_MCH_NO", default="")
JEEPAY_APP_ID = env("JEEPAY_APP_ID", default="")
JEEPAY_KEY = env("JEEPAY_KEY", default="")
#: 支付回调地址（Jeepay 主动 POST 到这里）。必须公网可达。
JEEPAY_NOTIFY_URL = env("JEEPAY_NOTIFY_URL", default="")

# ── 微信小程序支付（打手小程序）：AppID/AppSecret 只用于 `code2session` 换 openid。
# ⚠️ openid 按 AppID 隔离：公众号那串 openid 不能给小程序支付用（实测报「无效的 openid」）；
# 空 ⇒ `/api/app/wallet/wx-openid/` 与 `way=WX_LITE` 回业务错误（「小程序支付未配置」），⛔ 不 500。
WX_MINI_APP_ID = env("WX_MINI_APP_ID", default="")
WX_MINI_APP_SECRET = env("WX_MINI_APP_SECRET", default="")
# Jeepay 后台「小程序支付」那个**应用**自己的 appId / appKey（不是公众号那个 `JEEPAY_APP_ID`）。
# 商户号沿用 `JEEPAY_MCH_NO`（同一个商户）；网关 / 回调地址也沿用上面的 `JEEPAY_*`。
JEEPAY_MINI_APP_ID = env("JEEPAY_MINI_APP_ID", default="")
JEEPAY_MINI_APP_KEY = env("JEEPAY_MINI_APP_KEY", default="")
#: 小程序那个 Jeepay 应用可能挂在**另一个商户**下（实测：切到了某租户的另一商户
#: M1783678789，商户名已脱敏）。
#: 空 = 回落 `JEEPAY_MCH_NO`（应用与公众号同商户的形态）。
JEEPAY_MINI_MCH_NO = env("JEEPAY_MINI_MCH_NO", default="")

# ── Jeepay 支付宝代付（打手提现审核线）：上游代付通道的枚举值属「那台 Jeepay 实例配了什么」⇒ 落 env，
# 换环境改 `.env` 重启即可（⛔ 不改代码）；`ifCode` 是 `alipay` 还是 `aliaqfpay` 由实测决定，
# 实测前默认按社区版常见值 `alipay`。
JEEPAY_TRANSFER_IF_CODE = env("JEEPAY_TRANSFER_IF_CODE", default="alipay")
JEEPAY_TRANSFER_ENTRY_TYPE = env("JEEPAY_TRANSFER_ENTRY_TYPE", default="ALIPAY_CASH")
ALIPAY_AUTH_CONTROL_CREDENTIAL = env("ALIPAY_AUTH_CONTROL_CREDENTIAL", default="")
ALIPAY_AUTH_CONTROL_URL = env("ALIPAY_AUTH_CONTROL_URL", default="")
ALIPAY_AUTH_CONTROL_CLIENT_ID = env("ALIPAY_AUTH_CONTROL_CLIENT_ID", default="")
ALIPAY_AUTH_DATA_KEY = env("ALIPAY_AUTH_DATA_KEY", default="")
ALIPAY_AUTH_LOCAL_ENABLED = env.bool("ALIPAY_AUTH_LOCAL_ENABLED", default=False)
ONLINE_PAYROLL_ENABLED = env.bool("ONLINE_PAYROLL_ENABLED", default=False)
SETTLEMENT_CLIENT_KEYS = {"local": ALIPAY_AUTH_CONTROL_CREDENTIAL}
SETTLEMENT_CLIENT_NAMES = {"local": env("ALIPAY_AGENT_APP_NAME", default="")}
SETTLEMENT_FERNET_KEY = env("ALIPAY_AGENT_STATE_KEY", default="")
SETTLEMENT_OPERATION_LOCK_FILE = env("ALIPAY_AGENT_LOCK_FILE", default="")
SETTLEMENT_UPSTREAM_URL = "https://agent.jeepay.vip"
SETTLEMENT_USERNAME = env("ALIPAY_AGENT_USERNAME", default="")
SETTLEMENT_PASSWORD = env("ALIPAY_AGENT_PASSWORD", default="")
SETTLEMENT_MERCHANT = env("ALIPAY_AGENT_MERCHANT", default="")
SETTLEMENT_TESSERACT = env("ALIPAY_AGENT_TESSERACT", default="")
SETTLEMENT_ADAPTER = "apps.settlement_control.upstream.JeepayAdapter"

# ── 打手提现审核 / 代付的开关（业务方已拍板）──────────────
# `True` ⇒ 审核页出现「发起代付」动作、`/transfer/` 端点可用；默认 **False**（只能人工打款）。
# ⚠️ 上线顺序（计划 §6 / §9）：先 `=0` 人工打款跑通一笔 → 再用 0.1 元真单验一次代付 → 才开。
WITHDRAW_AUTO_TRANSFER = env.bool("WITHDRAW_AUTO_TRANSFER", default=False)
#: 单笔代付上限（元）。业务方拍板 = **1000**。超过 ⇒ `/transfer/` 直接拒
#: （业务码 VALIDATION，文案「超过代付单笔上限，请人工打款」）。
#: ⚠️ 判据用 `is not None`（E-0021）：`0` 是合法上限（任何一笔都超限），不是「没配置」。
WITHDRAW_TRANSFER_MAX_AMOUNT = env("WITHDRAW_TRANSFER_MAX_AMOUNT", default="1000")

# ── 自动代付的冷静期（`feat/wallet-single-payout` §2.1）：审核通过后等多少分钟才允许自动打款。
# 默认 4320=3 天（业务方口径）；验收要 1 分钟时只改这台机器 `.env`（`WITHDRAW_PAYOUT_DELAY_MINUTES=1`）不改代码。
# ⚠️ 判据用 `is not None`（E-0021）：0 是合法值（审核通过即到期），不是「没配置」。
WITHDRAW_PAYOUT_DELAY_MINUTES = env.int("WITHDRAW_PAYOUT_DELAY_MINUTES", default=4320)

# ── 超时自动审核（提现改造增量）──────────────────────────
# `pending` 超过多少小时没人处理 ⇒ 自动通过（进入上面的冷静期）。
WITHDRAW_REVIEW_TIMEOUT_HOURS = env.int("WITHDRAW_REVIEW_TIMEOUT_HOURS", default=24)
# 自动审核的**存量闸**（ISO 时间，例 `2030-01-02T00:00:00+08:00`）。
# ⚠️ **默认空 = 不自动审核**：不设下界会把上线前的历史 pending 单一次性全放行。
# 上线时在每台租户机的 `.env` 写部署当天。
WITHDRAW_AUTO_REVIEW_SINCE = env("WITHDRAW_AUTO_REVIEW_SINCE", default="")

# ── 客服后台调账的单笔上限（`feat/taker-wallet-adjust`）：客服给打手加/减余额或押金冻结，单笔默认
# 不超过 1000 元，超限 ⇒ `/api/business/wallets/<user_id>/adjust/` 直接拒（业务码 VALIDATION）。
# ⚠️ 判据用 `is not None`（E-0021）：0 是合法上限（= 一分钱也不许手工调），不是「没配置」。
WALLET_ADJUST_MAX_AMOUNT = env("WALLET_ADJUST_MAX_AMOUNT", default="1000")

# ── 提前结算申请额度（spec `2026-10-23-taker-income-and-early-settlement.md` §3.4）：每个打手最多申请
# 几次提前结算（申请过就算用掉）。默认 3，`.env` 可覆盖；读它一律用运行期
# `getattr(settings, "EARLY_SETTLE_LIMIT", 3)`（测试 override_settings 才生效），⛔ 不要抄常量进业务模块。
EARLY_SETTLE_LIMIT = env.int("EARLY_SETTLE_LIMIT", default=3)

# ── 会话 / CSRF 安全属性（spec §4.4）。刻意不设 SESSION_COOKIE_DOMAIN：host-only cookie 挡住浏览器跨租户携带（D5）。
SESSION_COOKIE_SECURE = not DEBUG          # Django 默认 False（global_settings.py:490），生产必须显式打开
SESSION_COOKIE_HTTPONLY = True             # Django 默认即 True（:494），显式写出免得被改动

SESSION_COOKIE_SAMESITE = "Lax"            # Django 默认（:497）
CSRF_COOKIE_SAMESITE = "Lax"               # Django 默认（:576）
# 与 SESSION_COOKIE_SECURE 同口径（登录也强制校验，D15）：Django 默认 False（global_settings.py:574），必须显式打开。
CSRF_COOKIE_SECURE = not DEBUG
# Django **支持**通配子域（middleware/csrf.py 的 allowed_origin_subdomains + is_same_domain）。
# 每个租户域名不必单独登记；部署时按根域给。
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
# 中间件层的 CSRF 失败默认渲染 HTML（`django/views/csrf.py`）。给登录接口显式加了
# `csrf_protect`（D15），这条路从此会对 API 请求命中——换掉默认实现，同一个 403 才只有
# 一种形状（信封），否则前端要按 Content-Type 分支处理两种 403（spec §4.4）。
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"

# ── 阿奇索（Agiso）开放平台：团团通作为「开发者」的唯一一套凭证 ──
# ⚠️ 这是**平台级**密钥，全平台共用；**不入库、不下发给租户**（spec §5.2）。
# 缺失时不得让服务起不来（本地无凭证也要能跑测试）⇒ 默认空串，读单端点会回 1015。
AGISO_APP_ID = env("AGISO_APP_ID", default="")
AGISO_APP_SECRET = env("AGISO_APP_SECRET", default="")
# 调用基址。默认值是**实测坐实**的（spec §3.2）；留成变量是为了便于对着沙箱/联调环境切换。
AGISO_API_BASE = env("AGISO_API_BASE", default="https://gw-api.agiso.com/aldsIdle")

# ── 闲管家（闲鱼店铺管理平台）开放平台：应用是租户自己建的，凭据按租户存租户 schema 的
# `XgjShopBinding` 表里（spec §3-D1），平台级只留基址开关；默认值实测坐实（spec §2.1）。
XGJ_API_BASE = env("XGJ_API_BASE", default="https://open.goofish.pro")

# 订单管理页「同步收款」的限速（笔/秒；计划 `2026-10-26-receipt-sync-button.md` §3-4）。
# ⚠️ 业务上限 3；实现 = 每次上游调用之间 `sleep(1/qps)`（时钟可注入，用例断言间隔）。
# 默认 2：闲管家按次计费，手动一次性任务不需要更快。
XGJ_RECEIPT_SYNC_QPS = env.int("XGJ_RECEIPT_SYNC_QPS", default=2)

# ── 闲管家自动发货节流队列的最小间隔（秒；D-0358）：worker 每发完一条睡这一档，单进程内任意时刻最多
# 一笔在飞（kexing 实测：上游同一秒放行 20 拒 33，默认档约 10 倍余量）。运行期 `xgj_ship._ship_min_interval`
# getattr 读取（override_settings 才生效）；≤0/NaN ⇒ 回落 1.0 + warning（⛔ 不按 0——节流失效）；字面写坏由 env.float 启动时抛。
XGJ_SHIP_MIN_INTERVAL = env.float("XGJ_SHIP_MIN_INTERVAL", default=1.0)

# ── 上家收款同步（D-0359）：相邻两次上游查询的间隔（秒；1–2 秒防鸡蛋铺限流，默认 1.5）。
# 运行期 `upstream_receipt_sync._interval_seconds` getattr 读取（同 XGJ_SHIP_MIN_INTERVAL 口径）；
# ≤0/NaN ⇒ 回落 1.5 + warning（⛔ 不按 0——节流失效、把上家打挂）。
RECEIPT_SYNC_INTERVAL_SECONDS = env.float("RECEIPT_SYNC_INTERVAL_SECONDS", default=1.5)
# 「查无」放弃阈值（夜间）：连续查无达到它 ⇒ 夜间候选不再收该单（列不清零，
# 到期语义由手动按钮兜底——手动跑不看这一列）。
RECEIPT_SYNC_NOTFOUND_GIVEUP = env.int("RECEIPT_SYNC_NOTFOUND_GIVEUP", default=3)
# 夜间完成时间窗（天）：只扫近 90 天完成的单（拍板⑤）。
RECEIPT_SYNC_WINDOW_DAYS = env.int("RECEIPT_SYNC_WINDOW_DAYS", default=90)

# ── 上下游订单互通（下家侧）：我方对外可达基址（spec §14-R13/裁决 R12）。生产 nginx 终止 TLS、本仓无 SECURE_PROXY_SSL_HEADER
# ⇒ request.scheme 恒 http，握手把它交给上家当推送地址；http:// 会被 301 且 httpx 不跟随重定向 ⇒ 下行推送永败 ⇒ 生产必须显式配
# `https://<租户域名>`（本地可留空走 `peer_views._local_base_url` 推导）。⛔ 别用 SECURE_PROXY_SSL_HEADER 代替（信任边界变更）；`src/tests/test_peer_sync_local_base_url.py` 守形态。
PEER_SYNC_LOCAL_BASE_URL = env("PEER_SYNC_LOCAL_BASE_URL", default="")
# ── 微信公众号集成（D-0268）：本机（租户服务器）公网 IP，公众号后台「IP 白名单」要填（设置页一键复制）。
# Django 探测不到"公网 IP"（部署侧的事）⇒ 只能部署显式配置；未配置 ⇒ 设置页显示"向部署方查询"占位，不影响功能。
MP_SERVER_PUBLIC_IP = env("MP_SERVER_PUBLIC_IP", default="")

# ── 鸡蛋铺图片下载单独一档超时（秒，默认 60）：SDK `JidanpuConfig.timeout` 管 JSON API，下载图片字节（`jidanpu_upstream_pics.download_pic`）
# 是另一条链路；线上那条「读订单图失败」是读图列表（ViewPic）超时、不是这一跳。运行期 `pic_timeout_seconds()` getattr 读取
# （override_settings 才生效），非整数/≤0 ⇒ 回落 60 + warning（⛔ 不静默按 0）；存原始字符串而非 env.int（写坏运行期回落，env.int 启动即抛）。
JIDANPU_IMAGE_TIMEOUT = env("JIDANPU_IMAGE_TIMEOUT", default="60")

# ── 鸡蛋铺读图列表（ViewPic）单独一档超时（秒）：`GET /OrderAjax/ViewPic`（「这张单有哪些图」的 JSON API）线上超时
# （SDK 默认 30 秒不够）⇒ 拍板单独 60 秒 + 共 2 次尝试；只影响 `fetch_remote_pics`/`remote_has_pic` 两个调用口，其余 JSON API 仍走 SDK 30 秒 + 全局重试。
# 运行期 `viewpic_timeout_seconds()` getattr 读取（非整数/≤0 ⇒ 回落 60 + warning）。与 `JIDANPU_IMAGE_TIMEOUT`（下载字节）两回事：一个读列表、一个下字节。
JIDANPU_VIEWPIC_TIMEOUT = env("JIDANPU_VIEWPIC_TIMEOUT", default="60")

# ── 鸡蛋铺①式状态回拉是否顺带覆盖同店启用中兄弟绑定的链路（默认开；兜底背景：对方关「开放平台接入权限」时②式全断、①式仍能读）。
# 开=同店启用中兄弟绑定（含②式）按远端 orderId 匹配写成「待验收」（判据/写面仍在 `jidanpu_member.pullable_links` 与
# `apply_remote_completion`，身份=链路自己的绑定）；关=只覆盖本绑定链路。停用的不覆盖（spec §8.4）；②式自己那条路不受影响。运行期 getattr 读取。
JIDANPU_MEMBER_PULL_COVERS_SIBLINGS = env.bool(
    "JIDANPU_MEMBER_PULL_COVERS_SIBLINGS", default=True
)

# ── 媒体文件存储（图片存储线，spec §6 / §12）──
# 可插拔开关：`local`（本机 + CI，**默认**，不联网、不需要云凭据）/ `cos`（生产）。
MEDIA_STORAGE_BACKEND = env("MEDIA_STORAGE_BACKEND", default="local")
TT_ORDER_FINANCE_ASYNC_EXPORTS = env.bool("TT_ORDER_FINANCE_ASYNC_EXPORTS", default=False)
TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS = frozenset(env.list("TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS", default=[]))
# 本机/CI 的落盘目录。`.tools/` 不进 git ⇒ 测试与开发的文件不会污染工作树。
MEDIA_ROOT = REPO_ROOT / ".tools" / "media"

# ── Django 允许读进内存的请求体上限（与本仓媒体上限不是同一条闸）：本机直传 `PUT /api/media/blob/<key>` 读
# request.body、Django 默认 2.5 MB 会 400（RequestDataTooBig）⇒ 抬到 20 MiB 盖住最大合法体 18 MiB；真闸仍在
# `media_blob_views`（Content-Length 前置判据 + 按 key 种类 10MB/18MiB，`BodySizeWallTests` 守，与 `FILE_UPLOAD_MAX_MEMORY_SIZE` 无关）。
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
# ⚠️ 全局设置、无 per-view 形式 ⇒ 所有读体端点（含未认证 login）一起抬、生产同样生效 ⇒ `NonDirectUploadBodyLimitMiddleware`
# 把非直传路径恢复 2.5 MiB ⇒ 400（E-0095）；无 Content-Length（分块）请求 WSGI 下体被读成空、RequestDataTooBig 永不触发——静默空体。

# 腾讯云 COS。⚠️ 默认空串：本机（local 模式）不该因为缺云凭据而起不来；
# 真值只进 `.env`（不进 git），仓库里只提交 `.env.example` 的占位（§4「不提交真实密钥」）。
COS_BUCKET = env("COS_BUCKET", default="")
COS_REGION = env("COS_REGION", default="")
COS_SECRET_ID = env("COS_SECRET_ID", default="")
COS_SECRET_KEY = env("COS_SECRET_KEY", default="")

# ── 请求耗时埋点（spec docs/plans/2026-10-04-request-metrics.md §5）：`RequestMetricsMiddleware` 每次请求往
# `RequestMetric` 写一行（谁、什么时候、哪个接口、多久）。`ENABLED`=总开关（默认开；关掉即整段埋点不写库，
# 临时排障/压测才关）；`SLOW_MS`=慢请求阈值（默认 500ms），耗时严格大于它的请求额外写一条 kind="slow" 行；agg 行不受阈值影响。
REQUEST_METRICS_ENABLED = env.bool("REQUEST_METRICS_ENABLED", default=True)
REQUEST_METRICS_SLOW_MS = env.int("REQUEST_METRICS_SLOW_MS", default=500)

# ── cache 配置（大厅读口短缓存）。⚠️ `default` 必须显式写：项目一旦定义 CACHES，未列出的 alias 直接
# `InvalidCacheBackendError`（django/utils/connection.py:61，实测 5.2.17，没有「未列出就回落」的内建机制）
# ⇒ 这里照抄默认形状，login_rate_limit / order_qr_fetch / dailiantong 等 `cache.` 用户的语义逐字不变。
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "wecom_archive_badges": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "wecom-archive-badges",
        "OPTIONS": {"max_entries": 1024},
    },
    # 大厅列表载荷专用（可达几百 KB）⇒ 独立 alias：与 default 里那些限流 / 拉取
    # 用户物理隔开，互相挤兑不伤限流。`max_entries` 压着总量
    # （几十个键 × 几百 KB ≈ 十几 MB/worker 封顶；两台生产 VM 内存只有 ~2GB）。
    "hall_list": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "hall-list-rows",
        "OPTIONS": {"max_entries": 64},
    },
}
if TT_SHARED_WEB_ROUTES_FILE:
    for cache_options in CACHES.values():
        cache_options["KEY_FUNCTION"] = "config.shared_web.shared_cache_key"

# 大厅列表读口的短缓存 TTL（秒）。`0` = 紧急关闭：读口直接透传 DB、零缓存读写
# （判据在 apps/core/short_cache.py::short_cached_payload）。
HALL_LIST_CACHE_TTL_S = env.int("HALL_LIST_CACHE_TTL_S", default=1)

# 绩效列表读口的聚合短缓存 TTL（秒）：count 与两个 SUM 合并算、按「筛选签名」缓存
# （判据在 apps/business/api/performance_views.py::_list_totals）。`0` = 紧急关闭，语义同上。
PERFORMANCE_LIST_CACHE_TTL_S = env.int("PERFORMANCE_LIST_CACHE_TTL_S", default=45)

# ── 测试期的密码哈希（全量耗时优化）：只影响 `manage.py test`，生产/开发服务器一行不变。
# Django 默认 PBKDF2 单次 50–150ms，测试里海量 `create_user` 累计可观；测试不验哈希强度 ⇒ MD5 语义等价、
# 快两个数量级。判据：`sys.argv` 出现 `test` 子命令（shard 脚本调的就是它）。
import sys as _sys  # noqa: E402  —— 本块只在文件末尾用一次，不放顶部免得与既有 import 排布打架

if "test" in _sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

    # ── 大厅短缓存在测试进程默认关：既有大厅用例大量是「GET→写（claim/退回大厅/造镜像行）→同参数立刻再 GET」，
    # 钉的是写路径语义，开着 1 秒缓存会吃到旧载荷。缓存自身判据在 `tests/test_hall_short_cache.py`
    # （依赖缓存开的用例都显式 override_settings TTL）。只影响 `manage.py test`：生产默认（1 秒）由上面的 env 行决定。
    HALL_LIST_CACHE_TTL_S = 0

    # ── 绩效列表聚合短缓存同理默认关：既有绩效用例同形（GET→写→同参数立刻再 GET），
    # 开着会吃到旧 total/summary。缓存自身判据在 `tests/test_performance_list_totals.py`
    # （依赖缓存开的用例显式 override_settings TTL）。生产默认（45 秒）由上面的 env 行决定。
    PERFORMANCE_LIST_CACHE_TTL_S = 0

    # ── 共享租户 schema（全量耗时优化）：`TenantTestCase` 每个测试类建一次 schema+跑一遍迁移，
    # 复用版让每个测试进程只建一次（细节与代价：`src/tests/support/fast_tenant.py`）。
    # 只影响 `manage.py test`；关掉：`FAST_TENANT_SCHEMA=0`。
    if env.bool("FAST_TENANT_SCHEMA", default=True):
        TEST_RUNNER = "tests.fast_tenant_runner.FastTenantRunner"

# `src/jidanpu/` —— vendor 进来的鸡蛋铺 SDK（**不要在这里改代码**）

## 这是什么

这个目录是 **`jidanpu-sdk` 的完整源码副本**（vendored），包名保持 `jidanpu` 不变，
所以在本仓里 `import jidanpu` 直接可用。它是**上游那个仓的产物**，不是本仓的代码。

⛔ **不要在本目录里改任何一行**（含 `spec/*.json`）。本目录唯一的「本仓自有文件」就是
**这份 `VENDORED.md`**；除了它，其余文件都与上游**逐字节相同**。
在这里改代码会同时失去两样东西：① 下次覆盖式更新会静默丢掉你的改动；
② 「SDK 的正确性只有一个落点」这条约定失效（见文末）。

## 来源（provenance）

| 项 | 值 |
|---|---|
| 上游仓库（本机路径） | `/Users/ghh/Projects/jidanpuSDK` |
| 上游分支 | `fix/login-device-info`（2026-10-20 起；基线是 `main` 的 `66edeed`） |
| 上游提交（完整） | `97aafc230bf8c8781168fd5a8b59abda7e01bedb` |
| 上游提交（短） | `97aafc2` |
| 上游工作区 | 干净（`git status --porcelain` 为空，2026-10-25 实测） |
| 版本 | `0.1.4`（2026-10-25 从 `0.1.3` 升；本版只有一条新增，见下表第 4 行） |
| 拷入日期 | 2026-10-25（上一次 2026-10-20） |
| 上游 git 远程 | **无**（`git remote -v` 为空，实测 2026-09-24）⇒ 上游目前只存在于本机 |

### 2026-10-25 版相对 2026-10-20 那次的差异（逐条）

| # | 文件 | 是什么 | 为什么 |
|---|---|---|---|
| 4 | `errors.py` / `transport.py` / `__init__.py` / `tests/test_transport_blocked.py` / `CHANGELOG.md` / `pyproject.toml` / `__init__.py`（版本） | **新增 `UpstreamBlockedError`**：HTTP `468`，以及「2xx 非 JSON 且自报 `SafeLine`」的拦截页，单独成类（不再落 `ApiError`）；版本 `0.1.3 → 0.1.4` | 使用方仓 2026-10-25 实测（`.tools/recon/rate-h2-*`，不进 git）：对端 WAF 的拒绝形态是 **468 + SafeLine**、**按来源 IP 全站封禁 5–15 分钟**。混进 `ApiError` 会被调度层当普通 4xx 重试 ⇒ 被封了还按原节奏撞（同族 E-0269）。⚠️ **不拿 `<html` 兜底**：2xx 回 HTML 也可能是登录页（会话掉了），那不是封禁 |

### 2026-10-20 版相对 2026-09-28 那次的差异（逐条）

| # | 文件 | 是什么 | 为什么 |
|---|---|---|---|
| 1 | `auth.py` / `tests/test_auth.py` / `CHANGELOG.md` / `pyproject.toml` / `__init__.py` | **登录表单 `deviceInfo` 从 `"bot"` 改成真实网页同形状**（`访问设备：PC，{"os":"windows",…}`）；版本 `0.1.2 → 0.1.3` | ⭐ 这是本次 vendor 的**唯一行为改动**：真网页的登录 JS 是 `'访问设备：' + … + JSON.stringify(layui.device())`，**从来没有 `bot`**。带着 `bot` 每次登录，等于向对端 WAF 自报机器人（现场：一台机被拦 IP、登录一律 HTTP 468）。使用方仓的证据与现场见 `docs/ERRORS.md` **E-0269** |
| 2 | `spec/*.json`、`registry.py`、`transport.py` 注释 | 上游 `0.1.2`（2026-10-07）那批：逐端点分页口径登记（`spec/pagination.json` 2→18 条、新增 `spec/pagination_probe.json`）、`Endpoint.pagination` 只读字段、`/OrderAjax/RecOrderByInternal` 的 `required`/`success_rule` 登记 | 顺带对齐：`0.1.2` 已在 `main` 上（`d1156e23` 那次 vendor 就带了它的一部分），本次按上游一处不落地重新拷，避免「半新半旧」 |
| 3 | `auth.py`（注释）、`tests/test_auth.py`（注释） | 把注释里的**错题编号** `E-0248` 改成不带编号的「使用方仓 `docs/ERRORS.md` 2026-10-20 那条」 | ⚠️ 那一版写死的 `E-0248` 在**使用方仓里是别的错题**（bash 3.2 那条），本线真正的错题是 **E-0269**。⇒ 上游⛔ 不再写死一个**会随合并顺延而漂**的号，改成按「日期 + 出处」指认（号码由使用方仓自己维护）。**纯注释改动、行为零变化**（`tests/test_auth.py` 55 条照过） |

⚠️ **`spec/endpoints.json` 的差异逐条核过**：没有新增 / 删除任何 `operation_id`，也没有新增
`required` 之外的参数（唯一一条 `required` 变化就是 `RecOrderByInternal` 的 `orderId`，而本仓调它时
本来就带 `orderId`）⇒ 对既有调用**不构成行为变化**。

拷入内容 = **两棵树**，共 22 个文件：

- 上游 `src/jidanpu/` → 本目录（`__init__.py`、`auth.py`、`client.py`、`config.py`、
  `errors.py`、`registry.py`、`transport.py`、`py.typed`、`models/{__init__,base,generated}.py`），
  **排除 `__pycache__`**；
- 上游 `spec/*.json` → 本目录下的 `spec/`（**11** 个文件，约 532 KB；2026-09-28 新增
  `spec/pagination.json`）。

### 本次（2026-09-28）登记进生成链的内容

来源：真机实测（内部抢单大厅「接单」与两处列表分页）。⛔ 生成物照旧**不手改**，
改上游输入 + 本仓 `docs/jidanpu-endpoints.json` 后重新生成：

- **`/OrderAjax/RecOrderByInternal`**（内部抢单大厅「接单」，写端点）：本仓
  `docs/jidanpu-endpoints.json` 该条的 `params` / `required` 登记为 `["orderId"]`
  （真机实测：`POST` + 表单 `data={"orderId": …}` ⇒ `code=1`「接单成功…」⇒ `orderId` 必填）
  ⇒ 生成的清单里 `params=[{name: orderId, location: body, required: true}]`
  （写接口位置记为表单 `body`），`param_names` 仍保留前端 JS 扒到的
  `["orderId", "__RequestVerificationToken"]`（后者由页面表单带；SDK **不**自动处理 CSRF）；
- **成功码**：上游 `spec/success_codes.json` 给它登记 `positive_code`（写接口口径
  `code > 0`；本次实测 `code=1`，登记的是**口径**不是字面值）⇒ 生成的清单带
  `success_rule: "positive_code"`；
- **分页口径**：上游新增输入 `spec/pagination.json`，登记 `/OrderAjax/GetMyOrderList`
  与 `/OrderAjax/GetInternalOrderGrabbingList`：`page` 从 1 起、**`limit` 不生效**
  （每页固定 20 行）、总行数在壳的 `count`、聚合 dict 在 `total`；生成器把它原样写进端点的
  `pagination` 字段，并新加 `Endpoint.pagination` 只读字段暴露给调用方。
  ⚠️ **它是描述性元数据**：`iter_all()` 不会自动按它翻页（默认 `page_size=100` 撞上
  「每页固定 20」的端点会在第一页就判「不满页」而提前结束）——要用就显式传
  `page_size=20`。这条限制已写进上游 `AGENTS.md` / `README.md` 与
  `spec/pagination.json` 的 `meta.note`，**未**改 `iter_all` 行为。

⚠️ **`spec/` 不是可选资源，漏了就直接坏**：`JidanpuClient` 在**构造时**从
`Path(__file__).resolve().parent / "spec"` 读端点索引，缺了会抛 `ConfigError`。
上游 `pyproject.toml` 的 `force-include` 注释把这条写成了硬要求。

## 本仓侧的两个运行期依赖

`src/pyproject.toml` 的 `dependencies` 里加了 `httpx>=0.27` 与 `pydantic>=2.7`
（下限**照抄上游 `pyproject.toml`**，不在本仓收紧；`pydantic` 是导入期硬依赖）。

## 更新步骤（上游改了以后怎么做）

1. **在上游仓改** `/Users/ghh/Projects/jidanpuSDK`：写代码 + 写测试，**不要在本目录改**。
   ⚠️ **生成物（`spec/*.json`、`docs/feature-index.md`）不要手改**：改上游输入 `docs/jidanpu-endpoints.json`，
   再用**显式 `--source`** 重新生成——默认 `SOURCE_INDEX` 是写死的**本机绝对路径**，指向**主检出**那份，
   而在本分支合并前主检出**尚未脱敏**，所以不能靠默认值：
   ```bash
   cd /Users/ghh/Projects/jidanpuSDK
   .venv/bin/python -m tools.import_endpoints --source <本仓>/docs/jidanpu-endpoints.json
   .venv/bin/python -m tools.build_feature_index
   ```
2. **在上游跑完它的门禁**（三项都要绿才允许拷过来；下面是本次 2026-09-28 的实测读数和
   2026-09-24 首次 vendor 时那组作对照）：
   ```bash
   cd /Users/ghh/Projects/jidanpuSDK
   .venv/bin/python -m pytest -q     # 本次：532 passed, 5 deselected（首次：519 passed, 5 deselected）
   .venv/bin/python -m ruff check .  # 本次：All checks passed!（首次同）
   .venv/bin/python -m mypy          # 本次：Success: no issues found in 10 source files（首次同）
   ```
3. **重新拷两棵树**（覆盖本目录；`spec/` 一并拷，理由见上）：
   ```bash
   rsync -a --delete --exclude '__pycache__' \
     /Users/ghh/Projects/jidanpuSDK/src/jidanpu/ <本仓>/src/jidanpu/
   rsync -a --delete /Users/ghh/Projects/jidanpuSDK/spec/ <本仓>/src/jidanpu/spec/
   ```
   → 覆盖后**本文件（`VENDORED.md`）也会被上游那份 `src/jidanpu/` 的 `--delete` 波及**
   （上游没有这个文件）⇒ 拷完请把它恢复，并**更新上面的提交号 / 版本 / 日期**。
4. **改本仓 `src/pyproject.toml` 的依赖下限**（仅当上游那两个下限变了）。
5. 在本仓跑门禁（`manage.py check` + 后端全套 + 前端 PII 真值守卫，
   守卫会扫 `src/**`，vendor 进来的 JSON 也在扫描面内）。

**上游若日后加了 git 远程**：把 URL 补进上面那张「来源」表——今天它没有远程，
这条记录是「上游只在本机」这个风险的唯一书面出口。

## 已知的真实值残留（**有意 defer**，不是漏改）

`spec/endpoints.json` 与 `spec/features.json` 的「**获取人员列表**」端点 recon note 里各有一个
**实测人员数（裸整数）**——那是真实租户的实测返回值。它**不进**本仓真值守卫的判据
（守卫只认形状与黑名单，裸整数两者都不命中），而本文件又规定 **`spec/*.json` 不许手改**
（改了就不再与上游逐字节等价）⇒ 登记为**有意 defer** 项：

- **处置**：⛔ 本仓**不动 vendored 的任何一行**；**上游 re-vendor 时与上游一并脱敏**
  （在**上游**改生成链的输入 / 输出，再按上面「更新步骤」覆盖式拷回来）。
- **代价（如实）**：这个数字会一直留在仓库里，直到那次 re-vendor 发生；在那之前，
  任何「`src/**` 真值守卫全绿」的结论**都不覆盖**这一处。
- 口径同源的登记：设计文档 `docs/specs/2026-09-24-jidanpu-sync-design.md` §12 第 18 条。

## 已知的与上游偏差（**当前为 0 处**；那 2 处「有意偏差」已于 2026-10-20 退役）

### 现行状态（2026-10-20 实测）

**本目录里除这份 `VENDORED.md` 之外，所有文件都与上游当前提交逐字节相同**（含
`transport.py` 与 `spec/success_codes.json`；实测逐文件 `diff -q` 全空，`services/` 与 `models/`
也是；唯一不同的是两侧的 `__pycache__` 字节码，⛔ 不进 git）。
⇒ 判「有没有人在这里偷偷改过」的直接办法：拿上游 `src/jidanpu/` + `spec/*.json` 与这里做
`diff -r`，**除 `__pycache__` / `VENDORED.md` 外必须为空**。

### 退役的那 2 处是什么（历史，⛔ 不是「现在还有」）

`transport.py` 与 `spec/success_codes.json` 里各有一处**指向本仓错题本**的编号引用，
上游原文写的是预留段号 `E-0100`。2026-09-28 那次 vendor 之后，本仓曾把这两处按当时的顺延号
（`E-0100` → `E-0084` → `E-0086` → `E-0087`，三次都是合并窗口按 `AGENTS.md` §5-F「未合并方让号」做的）
**就地改成 `E-0087`**，于是它们与上游不再逐字节等价——那就是当时登记的「有意偏差 2 处」。

### 怎么退役的（2026-10-20 覆盖式 re-vendor）

- **本次是覆盖式拷贝**（整棵 `src/jidanpu/` + `spec/*.json` 从上游重拷）⇒ 上游原文的 `E-0100`
  **被原样恢复**，那两处 `E-0087` 是本仓当时手改的，**没有任何上游提交承载它们**（上游从未接受
  那次改名）⇒ 覆盖之后就没了。**这不是事故，是那次手改的必然结局**——本文件当时就写着
  「⛔ 下次覆盖式 re-vendor 会把这两处静默冲掉」。
- ⚠️ **它是注释里的编号、对行为零影响**：`transport.py` 那一处只在注释里解释「空 `data` 算不算载荷」
  那条启发式的来历；`spec/success_codes.json` 那一处只在 `note` 字段里指向取证过程。**没有任何判据
  读这两个字符串**（成功码判据在 `success_rule` 的值上，不在 note 里）。
- ⛔ **不要再手工改 vendored 文件去「保住」那处补丁**：那正是上一次的做法，只会让下一次 re-vendor
  再冲一遍、并让「与上游逐字节相同」这条可验证性质失效。**真要改就回上游改**（见下一节）。
- 口径同源的登记：`docs/ERRORS.md` 的 **E-0087** 条目末尾那段「合并窗口顺延」note（历史记录照旧保留）。

## 为什么这么做（一句话）

**SDK 的正确性只有一个落点——上游那个仓**：用的时候发现缺陷，**回上游修 + 加测试 + 重新 vendor**，
不要在本仓包一层或直接改这里的源码绕过去。

> 依据：设计文档 `docs/specs/2026-09-24-jidanpu-sync-design.md` 的 **D3 / A9 / A14 / D15**
> （业务方 2026-09-24 指令「边发现 SDK 问题 边修复它」）。

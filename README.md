# Portfolio Monitor / Folio

中文个人行情工作台：市场概览、自选代码、历史图表与每日日报。Python 3.12 + uv + FastAPI + Jinja2 + HTMX + Chart.js。

## 先体验（不会连接真实行情）

```sh
uv sync --locked
DEMO_MODE=true uv run portfolio serve
```

打开 <http://127.0.0.1:8000>，点击进入演示。页面始终标记模拟数据。演示历史库 `data/demo-history.db` 与七天缓存 `data/cache.db` 分开；演示数据不会自动上传。

## 接入真实数据库

1. 在 Turso 创建 **libSQL 引擎数据库**，不是新 Turso 引擎数据库。应用使用官方 `libsql` 驱动，采用远程访问，不启用 embedded replica / 双向同步。
2. 复制 `.env.example` 为 `.env`，填写 `TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`。
3. 执行 `uv run portfolio hash-password`，将输出填入 `ADMIN_PASSWORD_HASH`。密码至少12位。用 `uv run python -c 'import secrets; print(secrets.token_urlsafe(48))'` 生成 `SESSION_SECRET`。
4. 设置 `DEMO_MODE=false`。填入需要的行情密钥，然后 `uv run portfolio serve`。
5. `uv run portfolio validate-providers` 可逐项验证真实覆盖；`uv run portfolio collect` 可手动采集并生成日报。

`.env` 不提交 Git。数据库地址/令牌只能由 Python 后端和定时任务访问，不能嵌入网页。生产使用 HTTPS、`APP_ENV=production` 和稳定会话密钥；生产禁止演示模式。

## 数据保存规则

- 云端 libSQL 是正式数据来源：完整行情、日报快照、产品、自选、配置及任务记录。
- 本地 `sqlite3` 文件只缓存**按设置时区计算的当天和前6个自然日**的行情、报告和任务记录；当前配置与产品元数据不受此期限影响。
- 先提交云端，再更新缓存。只清理缓存，绝不传播 DELETE 到云端。
- 旧历史直接查云端；近期缓存可重建。进程启动、采集完成及配置时区改变后清理；停机时不会运行清理。
- 历史日报为**首次成功生成的不可变快照**。当天重新采集更新产品行情，不改写已有报告；新增自选会在下一份日报出现。
- 国债收益率按百分点值保存，变化用 bp；价格与净值按百分比。价格为未复权收盘值，不等于总收益。
- 不保存份额、金额、交易记录，也不连接支付宝账号。基金通过基金代码与份额类别匹配公开净值。

## 数据源

| 类别 | 数据源 | 配置 | 当前覆盖说明 |
|---|---|---|---|
| 美国国债2/10/30年收益率 | FRED 官方公开CSV | 公开CSV无需密钥，亦支持 `FRED_API_KEY` | 通过实际采集命令验证；周末/假日可能无新数据 |
| 美国股票、ETF、现货黄金、原油参考报价 | Twelve Data | `TWELVE_DATA_API_KEY` | 目录不是免费权限承诺，取决于账号与标的覆盖 |
| 中国股票、指数、ETF、场外公募基金净值 | Tushare | `TUSHARE_TOKEN` | 需要相应接口权限，免费账号可能无法访问 |

内置 SPY、QQQ、上证指数、黄金、WTI及美国国债期限收益率作为市场概览；SPY/QQQ为ETF代理，不冒充指数本身。Twelve Data动态搜索限美国股票/ETF；中国股票搜索需要 `.SH`/`.SZ` 后缀，六位基金代码搜索由Tushare元数据确认。搜索结果需用户确认，接口失败不会自动切换到未经验证的数据源。

## 云端部署与调度

参见 [部署说明](docs/DEPLOYMENT.md)。仓库包含 Dockerfile、Render Blueprint、CI 与半小时检查到期的 GitHub Actions。

- 网页与任务连接同一个 Turso 库，不依赖电脑在线。
- `REPORTS_ENABLED=true` 仓库变量启用工作流；网页设置再启用定时，并选择 IANA 时区及时间，默认 `Asia/Tokyo` / `09:00` / 关闭。
- 工作流在 UTC 每小时07/37分检查，可能被平台延迟。首次到期且该日期无报告时生成；全失败不生成日报，下次再试。
- 工作流并发控制与数据库租约共同防重，禁止覆盖历史报告。
- Render免费服务的本地文件可能丢失，但本地只是缓存。免费服务休眠会导致首次打开较慢。
- 数据源、Turso及GitHub Actions均受免费额度限制。当前**不自动读取账户账单**；设置页提示去控制台查看额度，可通过 `CLOUD_STORAGE_USED_MB` / `CLOUD_STORAGE_LIMIT_MB` 录入存储用量，达到80%展示警示，不代表实时监控。不会自动升级套餐。

## 维护命令

```sh
uv run portfolio collect
uv run portfolio tick
uv run portfolio rebuild
uv run portfolio export --file backups/history-2026-10-18.json
uv run portfolio restore --file backups/history-2026-10-18.json
uv run portfolio validate-providers
uv run pytest -q
uv run ruff check .
```

导出为有版本号的JSON（不是SQLite文件），包含全部正式表的一致快照，不含令牌与任务锁。文件权限为600，不覆盖已有备份。恢复只能写入新的空历史库，避免覆盖真实数据；恢复后自动重建七天缓存。备份需另存独立安全位置，不能仅放在Render临时磁盘。

## 当前交付边界

代码可本地演示和自动测试；真实Turso往返、云端部署及受密钥保护的行情，需要你自己的账号和凭证才能验收。未连接真实云端时不会悄悄降级到本地历史库。完整验证记录见 [状态与验收](docs/STATUS.md)。

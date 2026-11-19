# watermark-bug-14 复现与二验（另纸）

## 一、病灶（两处静默缺陷）

| 位置 | 旧行为 | 后果 |
|------|--------|------|
| `app/services/refill.py` | `UPDATE run_batches ... WHERE table_name=?` 整表重写 | 已盖印行被新印值覆盖，等于「补漏写成整批重写」 |
| `app/services/gaps.py` | `gap = abs(expected - stamped) + 5` | 差额账凭空多 5，与漏印目录不同源 |
| `app/services/trace.py` | 边的来源未去重、无计数 | 多源汇到同一游时边/下游数字对不上 |

## 二、选边（已钉死，不再二选一）

题面给的两条落库命运，本实现选 **第一条**：

> 已写行只读留存；请求越界则**整笔回滚**（一行不落），并把这次请求整笔送**死信队列**。

不采用「先整笔回滚全批印记再按漏印集合重写」—— 那条路线会短暂抹掉已写印记，
一旦重写阶段失败，已写行即被误替。

## 三、旧版复现路径（在 `git stash` 后的旧代码上跑）

```powershell
git stash                      # 回到初始快照
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q            # 旧基线 4 条全绿，缺陷不在测试覆盖内
python run.py                  # http://127.0.0.1:7000
```

1. 登录 admin / 123456。
2. 观察批次表：`ord-20261113-01` 已是 `已盖印 / WM-ORD-131`。
3. `POST /api/batches/refill {"table_name":"dw_orders_d","watermark_value":"WM-NEW"}`。
4. **缺陷 1**：`ord-20261113-01` 的水印从 `WM-ORD-131` 被改成 `WM-NEW`，返回
   `rewritten: 2`（连已写行一起重写）。
5. `POST /api/gap-reports {"as_of_date":"2026-11-16"}`。
6. **缺陷 2**：应盖印 4、已盖印 2，摘要却写「缺口 7」——多出 5。

## 四、修复后二验路径（本机库可核）

```powershell
python -m pytest -q     # 25 passed
python run.py           # http://127.0.0.1:7000
```

页面底栏进「补写」，四处同看：**漏印目录 → 补写卡片（范围标记）→ 批次附页 → 回放进度**。

### 验收 1：补写只动漏印，已写行原样留存

```powershell
# 1) 取范围（应得 scope_ids=[1]，retained_ids=[4]）
curl.exe -b cj "http://127.0.0.1:7000/api/refill/preview?table_name=dw_orders_d&as_of_date=2026-11-16"
# 2) 补写
curl.exe -b cj -X POST -H "Content-Type: application/json" ^
  -d "{\"table_name\":\"dw_orders_d\",\"watermark_value\":\"WM-NEW\",\"scope_ids\":[1],\"as_of_date\":\"2026-11-16\"}" ^
  http://127.0.0.1:7000/api/batches/refill
# 3) 查库（本机 instance/watermark_lab.db）
sqlite3 instance\watermark_lab.db "select id,run_code,status,watermark_value from run_batches order by id;"
```

预期：`applied_ids=[1]`、`retained_ids=[4]`；id=4 仍是 `WM-ORD-131`。

### 验收 2：越界整笔拒绝 + 死信

```powershell
curl.exe -b cj -X POST -H "Content-Type: application/json" ^
  -d "{\"table_name\":\"dw_orders_d\",\"watermark_value\":\"WM-BAD\",\"scope_ids\":[1,4],\"as_of_date\":\"2026-11-16\"}" ^
  http://127.0.0.1:7000/api/batches/refill
```

预期：HTTP 409，`reason_code=STAMPED_READONLY`、`reason_chip=已写只读`、
`dead_lettered=true`、`applied_ids=[]`；`/api/dead-letters` 多出一条。**一行都没被写。**

### 验收 3：差额账与实印同源

```powershell
curl.exe -b cj -X POST -H "Content-Type: application/json" -d "{\"as_of_date\":\"2026-11-16\"}" ^
  http://127.0.0.1:7000/api/gap-reports
```

预期：`gap_count == expected_count - stamped_count == missing_count`，`consistent=true`。

### 验收 4：回放两轮数字对齐

对同一 `refill_run_id` 连打两次：

```powershell
curl.exe -b cj -X POST http://127.0.0.1:7000/api/refill/runs/1/replay
curl.exe -b cj -X POST http://127.0.0.1:7000/api/refill/runs/1/replay
```

预期：第一轮 `two_rounds_aligned=false`，第二轮 `true`，且页面「回放进度」
显示两轮 `补写/留存/范围` 数字一致；拒绝记录回放显示拒因芯片且 `applied_count=0`。

### 验收 5：双人并发抢补写

```powershell
python -m pytest -q tests/test_refill.py::test_concurrent_refill_only_one_wins
```

预期：两线程只有一个落档，另一个被判越界拒绝（`STAMPED_READONLY`）；
目标行水印等于胜者，不存在双写。

### 验收 6：只改目录计数不改补写范围 → 必须失败

```powershell
python -m pytest -q tests/test_refill.py::test_directory_scope_agrees_per_table
```

该测试逐表比对「目录漏印数」与「补写范围条数」，任何一侧口径漂移都会红。

## 五、落档不变量（代码内固定）

1. `UPDATE` 一律带 `status='待盖印'` 守卫，行数不符即整笔回滚。
2. 补写前后逐行比对已写快照（`status/watermark_value/stamped_at`），任何漂移判
   `STAMP_OVERWRITTEN` 并整笔拒绝。
3. 越界判定顺序：已写只读 → 已作废 → 非本表 → 不存在。
4. 写事务用 `BEGIN IMMEDIATE` 抢排他锁，`busy_timeout=10s`；WAL 下读写互不阻塞。
5. 拒绝档案与死信先 `COMMIT` 再抛错，保证死信一定可查。

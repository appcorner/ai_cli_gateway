# คู่มือการใช้งาน API: `mode` และ `session`

เอกสารนี้อธิบายการใช้งานพารามิเตอร์ **`gateway_options.mode`**, **`store_session`**, และ **`session_id`** บน AI CLI Gateway (`POST /v1/chat/completions`, `POST /v1/jobs`, และ `/admin/sessions`)

---

## 1. การเลือก `mode` (`gateway_options.mode`)

ทุกค่ายรองรับค่า `gateway_options.mode` ได้ 2 ค่า คือ `"chat"` และ `"agent"` (ในหน้า UI ของ `agy` และ `opencode` จะแสดง `"agent"` เป็น **Normal**)

```json
{
  "model": "opencode-default",
  "gateway_options": {
    "mode": "chat",
    "max_history_messages": 10
  },
  "messages": [
    { "role": "user", "content": "สรุปความแตกต่างของ REST กับ GraphQL สั้นๆ" }
  ]
}
```

### พฤติกรรมของแต่ละค่ายตาม `mode`

| Provider | ค่าเริ่มต้น (เมื่อไม่ส่ง `gateway_options`) | โหมด `"chat"` | โหมด `"agent"` (Normal) |
|---|---|---|---|
| **Codex (`codex`)** | `"chat"` (แต่ถ้าส่ง `session_id` มาด้วยจะใช้ `"agent"`) | ใช้ `CODEX_HOME` แยก (`chat-mode`), ปิด user config / skills / web search, ตั้ง reasoning & verbosity เป็น `low` | ใช้ config เต็มของ Codex (sandbox `read-only`, `approvalPolicy: never`) |
| **Antigravity (`agy`)** | `"chat"` | ส่ง `--disable-slash-commands` เพื่อปิดการเรียก slash commands และ skills | เปิดใช้งาน slash commands และ skills ตามปกติ |
| **OpenCode (`opencode`)** | `"chat"` | ส่ง `--pure` และตั้ง `OPENCODE_CONFIG_CONTENT={"permission":{"*":"deny"}}` เพื่อปิด plugins, tools และ skills ทั้งหมด | เปิดใช้งาน plugins, tools และ skills ตามปกติ |

### `max_history_messages` (ค่าเริ่มต้น `10`, ช่วง `1–50`)
- ใช้เฉพาะตอนคุยแบบ **ไม่ใช้ `session_id` (Stateless)** ในโหมด `"chat"`
- API จะตัดเฉพาะ `N` ข้อความล่าสุด (ไม่นับ `system`) ส่งให้ CLI เพื่อประหยัด context

> **Tip**: สามารถเช็กโหมดและความสามารถของแต่ละค่ายที่เปิดใช้งานอยู่ได้แบบ real-time ผ่าน `GET /v1/providers`

---

## 2. การจัดการ `session` (Stateless vs Persistent)

โดยค่าเริ่มต้น API เป็น **Stateless** (ไม่บันทึกบทสนทนาลงดิสก์) หากต้องการให้ CLI จำบริบทต่อเนื่องข้าม request ให้ใช้ `store_session` และ `session_id`

### 2.1 วงจรชีวิตของ Session (3 ขั้นตอน)

#### ขั้นตอนที่ 1: เริ่ม Session ใหม่ (`store_session: true`)
ส่ง `store_session: true` ใน request แรก ระบบจะคืน `session_id` กลับมาใน JSON response:

```bash
curl -sS http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agy-default",
    "store_session": true,
    "system_prompt": "ตอบเป็นภาษาไทยแบบกระชับ",
    "gateway_options": { "mode": "chat" },
    "messages": [
      { "role": "user", "content": "ช่วยวางแผนทำ API Gateway 3 ขั้นตอน" }
    ]
  }'
```

ตัวอย่าง Response (ตัดย่อ):
```json
{
  "id": "chatcmpl-...",
  "provider": "agy",
  "session_id": "123e4567-e89b-42d3-a456-426614174000",
  "choices": [
    { "index": 0, "message": { "role": "assistant", "content": "..." }, "finish_reason": "stop" }
  ]
}
```

#### ขั้นตอนที่ 2: คุยต่อใน Session เดิม (`session_id`)
นำ `session_id` ที่ได้มาส่งใน request ถัดไป โดย:
- **ข้อความสุดท้ายใน `messages` ต้องเป็น `role: "user"` เสมอ** (API จะส่งเฉพาะข้อความล่าสุดนี้ให้ CLI เพราะประวัติก่อนหน้าอยู่ใน thread ของ CLI แล้ว)
- **ส่ง `gateway_options.mode` ให้ตรงกับตอนสร้าง session** (สำคัญมากสำหรับ Codex เพราะ Codex แยกไฟล์เก็บ session ระหว่าง `chat` กับ `agent`)

```bash
curl -sS http://127.0.0.1:8088/v1/chat/completions \
  -H "Authorization: Bearer YOUR_GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "agy-default",
    "session_id": "123e4567-e89b-42d3-a456-426614174000",
    "gateway_options": { "mode": "chat" },
    "messages": [
      { "role": "user", "content": "ขยายความขั้นตอนที่ 2 ให้หน่อย" }
    ]
  }'
```

#### ขั้นตอนที่ 3: ดูรายการ อ่านประวัติ และลบ Session

| Endpoint | Method | หน้าที่ |
|---|---|---|
| `/admin/sessions` | `GET` | แสดง session ทั้งหมดของทุกค่ายที่เปิด `supports_sessions` |
| `/admin/sessions/{session_id}?provider={p}&mode={m}` | `GET` | อ่านประวัติข้อความใน session นั้น |
| `/admin/sessions/{session_id}?provider={p}&mode={m}` | `DELETE` | ลบ session และประวัติออกจากดิสก์ถาวร |

> **ข้อควรระวังเรื่อง Concurrency**: หากส่ง request เข้า `session_id` เดียวกันพร้อมกันหลายอัน สำหรับ Codex จะใช้ Redis session lock รอให้ turn ก่อนหน้าเสร็จก่อน

---

## 3. การใช้งานกับ Asynchronous Jobs (`POST /v1/jobs`)

หากคำสั่งใช้เวลานานกว่า `CHAT_WAIT_SECONDS` (ค่าเริ่มต้น 90 วินาที) ให้ใช้ `/v1/jobs` แทน ซึ่งรองรับทั้ง `gateway_options`, `store_session`, `session_id`, และ `system_prompt` เหมือนกัน:

```powershell
$headers = @{
    Authorization  = "Bearer YOUR_GATEWAY_API_KEY"
    "Content-Type" = "application/json"
}

$job = Invoke-RestMethod -Uri "http://127.0.0.1:8088/v1/jobs" -Method Post -Headers $headers -Body (@{
    model           = "opencode-default"
    prompt          = "ช่วยรีวิวแนวทางออกแบบระบบนี้"
    store_session   = $true
    gateway_options = @{ mode = "chat" }
} | ConvertTo-Json -Depth 4)

# รอเช็กผลลัพธ์และ session_id ที่ได้กลับมา
Invoke-RestMethod -Uri "http://127.0.0.1:8088/v1/jobs/$($job.id)" -Headers $headers
```

---

## 4. สรุปแนวทางการเลือกใช้

- **ถาม-ตอบทั่วไป / Automation ใน n8n ที่ไม่ต้องการจำบริบท**: ใช้ `mode: "chat"` และ **ไม่ต้องส่ง** `store_session` / `session_id` (เร็ว ปลอดภัย และไม่เปลืองพื้นที่เก็บ session)
- **คุยต่อเนื่องหลายรอบ (Multi-turn)**: ส่ง `store_session: true` ในรอบแรก แล้วนำ `session_id` มาใช้ต่อในรอบถัดไป โดยระบุ `mode` ให้ตรงเดิมเสมอ
- **งานที่ต้องการให้ CLI เรียก tools / skills / plugins ของตัวเอง**: ใช้ `mode: "agent"` (Normal)

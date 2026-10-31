# Venus 本机转发（venus_proxy）

CodeBuddy 自定义模型（`~/.codebuddy/models.json`）发出的请求只带 `Content-Type` 和 `Authorization` 两个请求头，配置文件里也没有能加请求头的字段，请求也只能走 OpenAI 兼容接口。结果是：Venus 的粘性路由开不了，Claude 的 prompt cache 用不上，Claude 也几乎不思考。这个转发程序装在 CodeBuddy 和 Venus 之间，补上这些，并且看住每一次流式回复，断流时给出明确的错误。它只用 Python 标准库，不需要装依赖。

```
CodeBuddy ──> http://127.0.0.1:18901/llmproxy/chat/completions ─┬─ Claude ──> /llmproxy/anthropic/v1/messages（原生接口，思考打开）
                                                                ├─ GPT-5 及以后 ──> /llmproxy/v1/responses（Responses 接口）
                                                                └─ 其他 ──> /llmproxy/chat/completions（原样转发）
```

## 它做什么

1. **按对话粘性路由**：CodeBuddy 每个请求都带 `X-Conversation-Id`，转发把它设成 `Venus-Session-Id`，同一对话固定落到同一个底层资源，prompt cache 才能命中。没有这个头时用 `Venus-Sticky-Routing: token`（按 API key）。请求里已经带了 `Venus-Session-Id` 或 `Venus-Sticky-Routing` 的话保持原样。`VENUS_PROXY_ROUTING=token` 可以改回全部按 key。
2. **Claude 走原生接口，思考打开**（见下文“为什么 Claude 走原生接口”）：model 名里带 `claude` 的请求，翻译成 Anthropic Messages 格式发给 `/llmproxy/anthropic/v1/messages`，带上 `thinking: adaptive` 和 `effort`（取请求里的 `reasoning_effort`，CodeBuddy 发的是 `high`），回复再翻译回 OpenAI 格式。思考内容放在 `reasoning_content` 里，CodeBuddy 会显示。
3. **把思考带回后续轮次**：CodeBuddy 不会把上一轮的思考放回历史。转发记住每次回复里带签名的思考块（按这一轮第一个工具调用的 id，或这一轮的正文），下一轮请求时放回原处，模型能接着上一步的思路往下做（Anthropic 的 interleaved thinking）。只存在内存里，最多 5000 轮。
4. **整理历史**（只在原生接口上）：去掉 CodeBuddy 在工具调用前填的占位正文 `"-"`；工具结果 `{"status":"success","result":{"stdout":"a\r\nb"}}` 展开成 `key: value` 的纯文本，换行不再被转义；缓存标记按 Anthropic 的上限放 4 个（工具、系统提示、最后两条用户消息），CodeBuddy 自己加的约 20 个丢掉（超过 4 个 Venus 会报 400）。
5. **原生接口不收就退回**：原生请求被 400/404/422 拒绝时，先去掉带回的思考再试一次（防止签名不被接受），还不行就走兼容接口（加上 `cache_control`，和以前一样）。日志里会记下 `fallback=`。
6. **每个请求记一行诊断日志**（见下文“日志怎么看”），能分清是 Venus 断的、Venus 卡住了，还是客户端自己走了。
7. **Venus 没接住请求时最多重试 3 次**：连不上，或者返回 429/502/503/504 时，依次等 5、20、40 秒重发（共 65 秒，跨过 Venus 按分钟算的 TPM 限流窗口）。这时客户端还什么都没收到，重发不会让回复重复。第 4 次还失败，才把 Venus 的原始错误交给客户端。
8. **Venus 卡住时主动中止**：流式回复超过 `VENUS_PROXY_STALL`（默认 270 秒）没有任何内容（原生接口的 `ping` 不算），就不再等了。
9. **流断了就明说**：流式回复没有 `finish_reason` 也没有 `[DONE]` 就结束了，转发会在流末尾补一个 `data: {"error": {...}}` 事件。CodeBuddy 收到后按“出错”处理，而不是把半截回复当成正常结束。
10. **留下失败请求的请求体**：上面这几种失败，CodeBuddy 发来的原始请求体会存到 `failed/`（权限 0600，只保留最近 5 个），方便直接拿去重放。只存请求体，不含 API key，但有完整对话内容。
11. **GPT-5 及以后走 Responses 接口**：model 名匹配 `gpt-5`～`gpt-9` 开头的请求（如 `gpt-6-sol`、`gpt-6.1-sol`），翻译成 Responses 格式发给 `/llmproxy/v1/responses`，回复再翻译回 OpenAI chat 格式。原因：这些模型在 `/chat/completions` 上**不接受工具和 `reasoning_effort` 同时出现**（`Function tools with reasoning_effort are not supported ... use /v1/responses`），而 CodeBuddy 每次都两样都带。同时：`reasoning.effort` 取请求的 `reasoning_effort`（`max` 映射成 `xhigh`），思考摘要放进 `reasoning_content`；加密的推理项（`store: false` + `reasoning.encrypted_content`）记下来，下一轮放回原处（和 Codex 一样）；`prompt_cache_key` 设成对话 id，提高缓存命中。加密推理只有签发它的 Venus 后端能解，被拒（400）时先去掉推理再试，还不行就退回 `/chat/completions`。

Kimi、DeepSeek、GPT-4 等模型原样转发，它们的缓存是自动的，有了第 1 条的粘性路由就够了。

转发只监听 `127.0.0.1`，别的机器连不上。它不保存也不打印 API key。

## 为什么 Claude 走原生接口

抓了 CodeBuddy 的真实请求，再对 Venus 做对照实验（2026-10-28）：

- 走兼容接口 `/chat/completions`，用 CodeBuddy 的请求形态（带工具历史），`reasoning_effort` 不传、`high`、`max`，或者 `thinking.budget_tokens`，**几乎都不思考**（`reasoning_content` 为空）。
- 走原生接口 `/anthropic/v1/messages`，`thinking: {"type": "adaptive"}` + `output_config: {"effort": "high"}`，每次都思考，并返回带签名的思考块；加 `display: "summarized"` 还能拿到思考摘要。
- CodeBuddy 不回传思考，转发替它补上。Venus 有 Anthropic 直连和 Bedrock 两个后端，实测一个后端签的思考块交给另一个后端也能用。

Cursor 走的就是原生接口，并且会回传思考。同一个模型在 CodeBuddy 里显得“笨”，主要就差在这里。

## 文件

| 文件 | 作用 |
|---|---|
| `venus_proxy.py` | 转发程序 |
| `claude_native.py` | OpenAI ↔ Anthropic 格式翻译、思考块的记忆 |
| `openai_responses.py` | OpenAI chat ↔ Responses 格式翻译（GPT-5 及以后） |
| `test_venus_proxy.py` | 测试 |
| `venus_proxy.service` | Linux systemd 配置模板（里面的 `DIR` 安装时替换成实际路径） |
| `com.venus.proxy.plist` | macOS launchd 配置模板（里面的 `DIR` 由 `install.sh` 替换成实际路径） |
| `install.sh` / `uninstall.sh` | macOS 安装 / 卸载开机自启 |
| `proxy.log` | 运行日志（安装后生成） |
| `failed/` | 最近几次失败请求的请求体（出现失败后才生成） |

## 安装

先把仓库放到 `~/.venus_proxy`：

```sh
git clone https://github.com/jetteezhou/venus_proxy.git ~/.venus_proxy
```

**Linux（systemd）**：开机自启，进程退出 5 秒后自动拉起。

```sh
sed "s#DIR#$HOME/.venus_proxy#g" ~/.venus_proxy/venus_proxy.service > /etc/systemd/system/venus-proxy.service
systemctl daemon-reload && systemctl enable --now venus-proxy
ss -lntp | grep 18901
tail -f ~/.venus_proxy/proxy.log
```

**macOS（launchd）**：随登录自动启动，进程退出会被自动拉起。

```sh
sh ~/.venus_proxy/install.sh
lsof -nP -iTCP:18901 -sTCP:LISTEN
tail -f ~/.venus_proxy/proxy.log
```

## 让 CodeBuddy 走转发

把 `~/.codebuddy/models.json` 里每个 Venus 模型的 `url` 从 `http://v2.open.venus.oa.com/llmproxy` 改成：

```json
"url": "http://127.0.0.1:18901/llmproxy"
```

CodeBuddy 会自己在后面加 `/chat/completions`。其他字段（`apiKey` 等）不用改。CodeBuddy 会热重载这个文件，保存后大约 1 秒生效，不用重启，也不用新建对话。改之前先备份，比如 `cp ~/.codebuddy/models.json ~/.codebuddy/models.json.bak`。

## 日志怎么看

每个请求结束时记一行，例如：

```
28/Oct/2026 22:28:32 #2 POST /llmproxy/chat/completions model=claude-opus-5-5 api=native route=session req=25249B status=200 thinking_restored=1/1 ttfb=3.6s dur=11.6s gap=2.8s resp=35437B stage=content finish=stop done=yes usage=in:4584,read:4424,write:158,out:1100,think:443 end=ok
```

| 字段 | 含义 |
|---|---|
| `api` | `native` Claude 原生接口；`native_unrestored` 原生接口但去掉了带回的思考；`compat` Claude 走兼容接口；`responses` / `responses_unrestored` GPT 走 Responses 接口（后者去掉了带回的推理）；`pass` 原样转发 |
| `route` | 粘性路由方式：`session`（按对话）、`token`（按 key）、`own`（请求自带） |
| `fallback` | 原生接口被拒、退回下一种方式的原因 |
| `thinking_restored` | 历史里有几轮助手回复被补回了思考 / 一共几轮。转发重启后、或这轮回复不是经转发原生接口生成的，补不回 |
| `backend` | Claude 这次落在 Venus 哪个后端（从消息 id 看出）：`anthropic` 直连、`bdrk` Bedrock、`vrtx` Vertex。各后端缓存互不相通，见“缓存为什么常常没命中” |
| `req` / `resp` | 请求体 / 回复的字节数 |
| `retried` | 最后一次失败的原因（状态码或异常名）和重试次数，如 `429*3` |
| `ttfb` / `dur` | 收到 Venus 响应头用的时间 / 整个请求用的时间 |
| `gap` | 回复过程中最长一次没有输出的时间。Claude 写工具参数时会先停一阵，再一次性吐出参数，所以这个值通常就是生成最大那个工具参数的时间 |
| `stage` | 最后一段输出是什么：`content` 正文、`reasoning_content` 思考、`tool_calls` 工具调用 |
| `finish` / `done` | 回复的 `finish_reason`，以及有没有收到 `[DONE]` |
| `usage` | 输入 / 缓存命中 / 缓存写入 / 输出 / 其中思考 token |
| `err` | Venus 返回的错误信息 |
| `end` | 怎么结束的，见下表 |
| `saved` | 失败请求体存到了哪里 |

| `end` | 意思 | 谁的问题 |
|---|---|---|
| `ok` | 正常结束 | — |
| `upstream_eof` | Venus 在没给 `finish_reason` / `[DONE]` 的情况下关了连接 | Venus |
| `stalled` | 超过 `VENUS_PROXY_STALL` 秒 Venus 没有任何输出，转发主动中止 | Venus |
| `upstream_error:…` | 读 Venus 回复时连接出错 | Venus 或网络 |
| `no_answer:…` | 重试后仍连不上 Venus，返回 502 | Venus 或网络 |
| `client_gone` | 客户端（CodeBuddy）先断开了，转发随即断开 Venus，Venus 停止生成 | 客户端，比如用户点了停止或客户端超时 |

找问题时：

```sh
grep -v 'end=ok' ~/.venus_proxy/proxy.log | tail          # 只看出问题的请求
grep 'model=claude' ~/.venus_proxy/proxy.log | grep -oE 'api=[^ ]+|thinking_restored=[^ ]+|usage=[^ ]+' | paste - - - | tail   # 看思考与缓存
grep -o 'fallback=[^ ]*' ~/.venus_proxy/proxy.log | sort | uniq -c   # 原生接口被拒的原因
```

重放一个失败请求（直连 Venus 兼容接口，看是不是 Venus 自己的问题）：

```sh
curl -sN http://v2.open.venus.oa.com/llmproxy/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Venus-Sticky-Routing: token' -d @$HOME/.venus_proxy/failed/<文件名>.json | tail -5
```

## 缓存为什么常常没命中（2026-10-31 查的）

把 `proxy.log` 里 10-28～10-31 的 Claude 请求按“上一轮之后多久发出”分开统计：

- 间隔超过 5 分钟（Anthropic 缓存的 TTL）：约 90% 未命中，这是 TTL 到期，正常。
- 间隔不到 5 分钟：约 20% 未命中，原生接口（按对话路由）和以前的兼容接口（按 key 路由）一样。整段几十万 token 重新按 1.25 倍写入缓存，写入费用主要就花在这里。

这 20% 不是请求的前缀变了：对比 `failed/` 里同一对话前后两次请求，CodeBuddy 发来的历史逐条一致，工具和系统提示也没变，转发补回的思考块数也只增不减。日志的规律是“一次未命中、下一次正好命中它刚写的缓存”，有时候读到的是好几轮之前的前缀，也就是说，同一个对话被 Venus 分到了缓存互不相通的不同后端。实测 Venus 的 Claude 后端至少有 Anthropic 直连（消息 id `msg_01…`）和 Bedrock（`msg_bdrk_…`）两个。单独连发同一个请求时，按 session 和按 token 都能稳定命中，所以后端切换多半发生在 Venus 负载高、某个后端报错时（这段时间也偶尔能看到 Venus 直接回 502）。

转发没法指定后端，只能记录：日志里的 `backend=` 字段记的就是每次落在哪个后端。核对办法：

```sh
grep 'api=native' ~/.venus_proxy/proxy.log | grep -oE 'backend=[^ ]+|usage=in:[0-9]+,read:[0-9]+' | paste - - | tail -40
```

如果未命中正好落在 `backend` 变化的那一次，就能确认是 Venus 侧的问题，要找 Venus 的同学按会话固定后端。1 小时 TTL 的缓存帮不上忙：写入按 2 倍计价，按现在的未命中比例算反而更贵。

GPT 走 Responses 接口时，`prompt_cache_key` 设成对话 id，实测同一对话第二轮起就能命中前缀（`read=` 接近 `in=`）。

## 为什么 STALL 默认 270 秒

CodeBuddy 给每个工具调用设了 300 秒的计时：从收到工具名开始算，300 秒内参数还没收全，就取消这个工具调用，提示 `Your response ended with finish reason "unknown" before completing this tool call; please regenerate.`。这时它会把整个回复当成“正常结束”，不重试，也不报错。

实测 Venus 返回 Claude 的工具调用时，工具名立刻就发下来了，参数却要等全部生成完才一次性发出（大约每 KB 参数 2.5 秒，兼容接口、原生接口、开不开 `fine-grained-tool-streaming` 都一样；原生接口中间只有 `ping`）。所以只要 Venus 卡住，或者模型在一个工具参数里写得很长，CodeBuddy 就会在第 300 秒取消这个工具调用。

把 STALL 设成 270 秒，Venus 卡住时转发会抢在 CodeBuddy 之前中止，并给出明确的错误，CodeBuddy 会显示“出错”而不是“unknown”。代价是：一个超过约 100 KB 的工具参数（正常写文件很少这么大）也会被当成卡住。这种情况即便不中止，CodeBuddy 也会在 300 秒时取消它，所以不会多失败一次。

转发管不了的：CodeBuddy 的 300 秒计时是按“工具名到参数收全”算的，转发补的心跳或空字节都不会重置它。避免这类失败最有效的办法是让单个工具调用小一些（大文件分几次写），以及上下文别太长（越长 Venus 越容易慢）。

## 常用操作

```sh
systemctl restart venus-proxy                          # Linux：重启（改了代码之后）
launchctl kickstart -k gui/$(id -u)/com.venus.proxy    # macOS：重启
cd ~/.venus_proxy && python3 -m unittest -v test_venus_proxy   # 跑测试
```

可以用环境变量调整：

| 变量 | 默认 | 作用 |
|---|---|---|
| `VENUS_PROXY_PORT` | `18901` | 监听端口 |
| `VENUS_PROXY_UPSTREAM` | `http://v2.open.venus.oa.com` | 上游地址 |
| `VENUS_PROXY_STALL` | `270` | 流式回复中 Venus 多少秒没有输出就中止 |
| `VENUS_PROXY_KEEP_FAILED` | `5` | 保留最近几次失败请求的请求体，`0` 不保留 |
| `VENUS_PROXY_CLAUDE_NATIVE` | `1` | `0`：Claude 改回走兼容接口（只加 `cache_control`），即以前的行为 |
| `VENUS_PROXY_CLAUDE_EFFORT` | 跟随请求 | 强制 Claude 的思考档位：`low` / `medium` / `high` / `xhigh` / `max`，`none` 关闭思考 |
| `VENUS_PROXY_GPT_RESPONSES` | `1` | `0`：GPT-5 及以后改回原样走 `/chat/completions`（带工具时会被 Venus 拒绝） |
| `VENUS_PROXY_ROUTING` | `session` | `token`：全部按 API key 粘性路由，即以前的行为 |

Linux 上写进 `/etc/systemd/system/venus-proxy.service` 的 `[Service]`（如 `Environment=VENUS_PROXY_STALL=200`），然后 `systemctl daemon-reload && systemctl restart venus-proxy`。macOS 上写进 plist 的 `EnvironmentVariables`，然后重新运行 `install.sh`。

## 卸载

先让 CodeBuddy 改回直连，否则它会连不上：

```sh
cp ~/.codebuddy/models.json.bak ~/.codebuddy/models.json   # 或者手动把 url 改回 http://v2.open.venus.oa.com/llmproxy
systemctl disable --now venus-proxy && rm /etc/systemd/system/venus-proxy.service   # Linux
sh ~/.venus_proxy/uninstall.sh                                                      # macOS
```

## 需要知道的

- **转发停了，CodeBuddy 就连不上**，会报连接错误。systemd / launchd 会自动拉起它，如果一直起不来，看 `proxy.log`。
- **重启转发会打断正在生成的回复。** 所以改代码后找个空闲的时候再重启。重启还会丢掉记住的思考块：正在进行的对话下一轮补不回以前的思考，前缀变了，会多一次缓存未命中。
- **按对话粘性路由**：一个对话固定在一个底层资源上，不同对话可以分散开。按 token 时同一个 key 的所有对话都挤在一个资源上，同时开很多对话时更容易慢或 429。
- **只在客户端还没收到任何东西时重试**。回复已经发出一部分再断，转发不会重发（重发会让回复重复，工具也可能被执行两次），只会补一个错误事件，由客户端决定是否重新生成。
- **思考更多，回复会慢一些、输出 token 会多一些**：这是让 Claude 想清楚再动手的代价。嫌慢可以设 `VENUS_PROXY_CLAUDE_EFFORT=medium`。
- **缓存有门槛**：缓存区间要超过 1024 token 才生效。缓存写入按 1.25 倍计价，命中按 0.1 倍计价。上下文长、轮数多的对话最省钱。
- **Claude 是靠模型名认的**：model 名里带 `claude` 才走原生接口。以后接入名字里不带 `claude` 的 Claude 模型，要改 `venus_proxy.py` 里的判断。
- **原生翻译只覆盖 CodeBuddy 用到的部分**：文字、图片（data URL 或网址）、工具调用、`tool_choice`、`stop`、流式和非流式。其他 OpenAI 参数（`n`、`logprobs`、`response_format` 等）会被忽略；需要的话设 `VENUS_PROXY_CLAUDE_NATIVE=0`。
- **只处理 `/chat/completions`**：其他路径照样转发。
- **`failed/` 里是完整对话内容**，权限是 0600，只保留最近几个；不想留就设 `VENUS_PROXY_KEEP_FAILED=0`。

## 和 paper2brain 的关系

paper2brain 的 DeepSeek Harness 任务不走这个转发。那条链路由 `paper2brain-client/p2b_client/dsh.py` 的 `_venus_patch` 在 dsh 配置里直接加请求头（`Venus-Session-Id` 按 agent 区分，Responses 接口再加 `Venus-Codex-Routing`），Claude 请求则打开 `cacheControlFormat: anthropic`。两者互不依赖。

# Venus 本机转发（venus_proxy）

CodeBuddy 自定义模型（`~/.codebuddy/models.json`）发出的请求只带 `Content-Type` 和 `Authorization` 两个请求头，配置文件里也没有能加请求头的字段。所以 Venus 的粘性路由开不了，Claude 的 prompt cache 也用不上。这个转发程序装在 CodeBuddy 和 Venus 之间，替 CodeBuddy 补上这两样东西，并且看住每一次流式回复，断流时给出明确的错误。它只用 Python 标准库，不需要装依赖。

```
CodeBuddy ──> http://127.0.0.1:18901/llmproxy/chat/completions ──> http://v2.open.venus.oa.com/llmproxy/chat/completions
                     （加请求头，Claude 请求加 cache_control，看住流式回复）
```

## 它做什么

每个请求原样转发：路径、`Authorization`、请求体、流式返回（SSE）都不改动，只额外做这些事：

1. **加粘性路由请求头 `Venus-Sticky-Routing: token`**：按 API key 固定路由到同一个底层资源，prompt cache 才能命中。请求里已经带了 `Venus-Session-Id` 或 `Venus-Sticky-Routing` 的话，就保持原样不覆盖。
2. **Claude 请求加 `cache_control: {"type": "ephemeral"}`**：model 名里带 `claude` 的请求才加，加在三个位置：系统提示、最后一个工具、最后一条带文字的消息。这三个位置和 DeepSeek Harness 的 `cacheControlFormat: anthropic` 一致。请求里已经有 `cache_control` 字段（客户端自己加的）就不再加。正文里只是提到 `cache_control` 这个词，不算已加。
3. **每个请求记一行诊断日志**（见下文“日志怎么看”），能分清是 Venus 断的、Venus 卡住了，还是客户端自己走了。
4. **Venus 没接住请求时重试一次**：连不上，或者返回 429/502/503/504 时，等 2 秒重发一次。这时客户端还什么都没收到，重发不会让回复重复。第二次还失败，就把 Venus 的原始错误交给客户端。400 等请求本身的错误不重试。
5. **Venus 卡住时主动中止**：流式回复超过 `VENUS_PROXY_STALL`（默认 270 秒）没有任何字节，就不再等了。
6. **流断了就明说**：流式回复没有 `finish_reason` 也没有 `[DONE]` 就结束了（Venus 断开、卡住被中止），转发会在流末尾补一个 `data: {"error": {...}}` 事件。CodeBuddy 收到后按“出错”处理，提示“Venus 的流式回复中途断了……请重试”，而不是把半截回复当成正常结束。
7. **留下失败请求的请求体**：上面这几种失败，请求体会存到 `failed/`（权限 0600，只保留最近 5 个），方便直接拿去重放。只存请求体，不存请求头，所以不含 API key。注意请求体里有完整对话内容。

GPT、Kimi、DeepSeek 等模型的缓存是自动的，不需要改请求体，有了第 1 条的粘性路由就够了。

转发只监听 `127.0.0.1`，别的机器连不上。它不保存也不打印 API key。

## 文件

| 文件 | 作用 |
|---|---|
| `venus_proxy.py` | 转发程序 |
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
28/Oct/2026 21:05:13 #12 POST /llmproxy/chat/completions model=claude-opus-5-5 req=511703B status=200 ttfb=3.4s dur=22.1s gap=11.8s resp=48213B stage=tool_calls finish=tool_calls done=yes usage=in:130301,read:128840,write:1450,out:812 cache_control=added end=ok
```

| 字段 | 含义 |
|---|---|
| `req` / `resp` | 请求体 / 回复的字节数 |
| `retried` | 重试过一次，以及第一次失败的原因（状态码或异常名） |
| `ttfb` / `dur` | 收到 Venus 响应头用的时间 / 整个请求用的时间 |
| `gap` | 回复过程中 Venus 最长一次没有输出的时间。Claude 写工具参数时会先停一阵，再一次性吐出参数，所以这个值通常就是生成最大那个工具参数的时间 |
| `stage` | 最后一段输出是什么：`content` 正文、`reasoning_content` 思考、`tool_calls` 工具调用 |
| `finish` / `done` | Venus 给的 `finish_reason`，以及有没有收到 `[DONE]` |
| `usage` | 输入 / 缓存命中 / 缓存写入 / 输出 token |
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
grep 'model=claude' ~/.venus_proxy/proxy.log | grep -o 'usage=[^ ]*' | tail   # 看缓存命中
```

重放一个失败请求（直连 Venus，看是不是 Venus 自己的问题）：

```sh
curl -sN http://v2.open.venus.oa.com/llmproxy/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Venus-Sticky-Routing: token' -d @~/.venus_proxy/failed/<文件名>.json | tail -5
```

## 为什么 STALL 默认 270 秒

CodeBuddy 给每个工具调用设了 300 秒的计时：从收到工具名开始算，300 秒内参数还没收全，就取消这个工具调用，提示 `Your response ended with finish reason "unknown" before completing this tool call; please regenerate.`。这时它会把整个回复当成“正常结束”，不重试，也不报错。

实测 Venus 返回 Claude 的工具调用时，工具名立刻就发下来了，参数却要等全部生成完才一次性发出（大约每 KB 参数 2.5 秒，开不开 `fine-grained-tool-streaming` 都一样）。所以只要 Venus 卡住，或者模型在一个工具参数里写得很长，CodeBuddy 就会在第 300 秒取消这个工具调用。

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
- **重启转发会打断正在生成的回复。** 所以改代码后找个空闲的时候再重启。
- **按 token 粘性路由，并发会受影响**：同一个 key 的所有对话都会落到同一个底层资源，同时开很多对话时可能变慢，或者遇到 429。转发会重试一次。
- **只在客户端还没收到任何东西时重试**。回复已经发出一部分再断，转发不会重发（重发会让回复重复，工具也可能被执行两次），只会补一个错误事件，由客户端决定是否重新生成。
- **缓存有门槛**：缓存区间要超过 1024 token 才生效。缓存写入按 1.25 倍计价，命中按 0.1 倍计价。上下文长、轮数多的对话最省钱。
- **Claude 是靠模型名认的**：model 名里带 `claude` 才加缓存标记。以后接入名字里不带 `claude` 的 Claude 模型，要改 `add_cache`。
- **只处理 `/chat/completions`**：其他路径照样转发，但不加缓存标记。
- **`failed/` 里是完整对话内容**，权限是 0600，只保留最近几个；不想留就设 `VENUS_PROXY_KEEP_FAILED=0`。

## 和 paper2brain 的关系

paper2brain 的 DeepSeek Harness 任务不走这个转发。那条链路由 `paper2brain-client/p2b_client/dsh.py` 的 `_venus_patch` 在 dsh 配置里直接加请求头（`Venus-Session-Id` 按 agent 区分，Responses 接口再加 `Venus-Codex-Routing`），Claude 请求则打开 `cacheControlFormat: anthropic`。两者互不依赖。

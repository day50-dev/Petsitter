# User guide

A walkthrough: install, connect a tool, see its traffic, add extensions, split
traffic into channels, and a few recipes.

[← back to the README](../README.md)

---

## 1. Install and start

```bash
uvx petsitter
```

Or install it:

```bash
pip install petsitter
petsitter
```

From a checkout of the repo:

```bash
./petsitter
```

It listens on `localhost:8080` and opens the dashboard in your browser. To use
another address or port:

```bash
petsitter -l localhost:9000
```

`--no-browser` skips opening the browser. Settings are saved in
`~/.config/petsitter/` (or `$PET_CONFIG_DIR`); `-c` points at another config.
See the [command line reference](cli.md).

The dashboard's sidebar has **Start here**, **Connecting**, **Agents**,
**Models**, your **Channels**, and **Help** at the bottom. Start here walks
through the next three steps.

To stop: the exit button at the top right, or Ctrl-C. Either one puts back any
tool config petsitter changed.

## 2. Set your model and connect a tool

### Set your provider

Open **Models** (or step 1 on Start here). Pick a provider, paste your key,
pick a model from the list. Picking a model saves it.

Not in the list? Pick **Something else** and enter its base URL.

### Point a tool at petsitter

Two ways.

**Let petsitter do it.** Open **Agents**. Claude Code, Codex and OpenCode are
listed. Press **Connect** on one. Petsitter edits that tool's own settings file
to point at itself, and creates a channel for it with these extensions:
Context Monitor, Secrets Protector, Rules File, Export It, Tool Dashboard.
Switch it off (or exit petsitter) and the file goes back as it was.

| Tool | File it edits | Channel it creates |
|------|---------------|--------------------|
| Claude Code | `~/.claude/settings.json` (`ANTHROPIC_BASE_URL`) | `claude-code`, Model `claude*` |
| Codex | `~/.codex/config.toml` (`openai_base_url`) | `codex`, Model `gpt*` |
| OpenCode | `~/.config/opencode/opencode.json` (`baseURL`) | `opencode`, X-Title `opencode*` |

Claude Code keeps talking to Anthropic with its own login. Petsitter never
needs that key.

**Do it yourself.** Open **Connecting** and copy the base URL:

```
http://localhost:8080/v1
```

In your tool, pick the **OpenAI compatible** or **local** provider and paste it.
For a program that only reads an environment variable:

```bash
OPENAI_BASE_URL=http://localhost:8080/v1 your-program
```

Requests sent here go to the model you set on the Models page, whatever model
the tool asks for.

## 3. See what your tool sends

On a fresh install, the **Default** channel already has Context Monitor, Tool
Dashboard, Secrets Protector and Export It.

### Context Monitor

Step 3 on Start here, **Open Context Monitor**, opens its **Live** tab. Use your
tool as normal. Each request shows up. Click one to see:

- where it came from: address, X-Title, User-Agent, channel, model
- what the context is made of: system prompt, tool definitions, conversation,
  tool results, in estimated tokens
- the conversation's size over time, and where it was compacted
- the full system prompt, the tools offered, and every message

### Tool Dashboard

Open it from the channel's **Extensions** tab, then its **Live** tab. It shows
every tool your tool offers the model and every call the model makes, with the
arguments and output. Click a tool to see its calls; **download** saves a call's
full arguments and output as JSON.

Each tool has a switch. Turn one off and the model isn't offered it from the
next request on, even a tool built into your tool. The setting lasts across
restarts.

No tool connected yet? Press **demo** on the Live tab.

If you connected a tool from the Agents page, its requests go to its own
channel, so open these from that channel.

## 4. Extensions

Pick a channel in the sidebar, then the **Extensions** tab.

- **Browse** lists what you can install: the bundled ones and community ones.
  Click one to read about it, then **Install**. Search filters both lists.
- **Installed** lists what this channel runs.

An installed extension's page has:

- **Live**: its own page, if it has one (Context Monitor, Tool Dashboard,
  Context Editor...). **Pop out** opens it in its own browser tab.
- **Details**: what it does, how to use it, its current settings.
- **Source code**: for bundled and community extensions.
- **Settings**: change its options.
- **On/Off**: turn it off without uninstalling. The same switch is on each row
  of the Installed list.
- **Uninstall**.

### Run order

Installed extensions run in the order shown, 1 first, on every request. Drag
one by its handle to change when it runs.

Order matters. An extension that watches (Context Monitor, Tool Dashboard,
Traffic Logger) sees the request as the extensions before it left it. Put it
first to see what your tool really sent.

### The "!"

A "!" next to an extension means it isn't fully working. Hover it, or open the
extension, for what's wrong and how to fix it. The channel gets a "!" too. For
example, Secrets Protector shows one when the `detect-secrets` library is
missing.

A "!" on **Connecting** means petsitter couldn't reach your provider; the error
is on that page. A "!" on **Default** means another channel matches every
request, so Default never runs.

### Try it

The speech-bubble button at the top opens **Try it**. Type a message and it goes
through the selected channel's extensions to your model, the same way a real
request does.

Each reply shows a pill per extension: bright if it changed something (hover for
which stages ran), dim if it did nothing, and why a keyword-gated one didn't
fire. Extensions that changed something also light up in the Installed list.

**Table** gives the model three tools on a small key/value table kept in your
browser: `get_table`, `get_value(key)`, `set_value(key, value)`. The panel runs
the calls itself. Use it to see what an extension does to tool calls and their
results without a real agent. **Tools on/off** turns them off.

## 5. Channels

A channel is a set of extensions for some of your traffic. Use one when
different programs need different extensions.

A channel matches requests by three fields. All of them must match:

- **X-Title header**: the name the program sends, if it sends one
- **User-Agent**: useful when it sends no X-Title
- **Model**: the model named in the request

Case doesn't matter. `*` matches anything (`claude*`). Empty means `*`.

A request goes through every channel that matches it. **Default** gets what no
channel matches. A matching channel claims the request even when it's empty or
its extensions are off.

### Make one from real traffic

Open **Agents**. **Discovered programs** lists every program that has sent
requests through petsitter: its X-Title (or User-Agent), its models, and which
channel its requests went to. Click a row to see the headers it sends.

Press **New channel for this**. The form is filled in from that program. As you
type, it says which of the programs seen so far it would catch.

Or press **Add channel** in the sidebar and fill it in yourself.

### Change or remove one

Select the channel and press **Edit channel** in the bar at the top. From there:
**Save**, **Turn off** (kept, but no longer applies), **Delete channel**.

Each channel also has:

- **Logs**: the activity log, and the channel's own log file.
- **Settings**: the channel's log level and log file, and the models it uses.

**Help** has **Install Examples**, which adds example channels, turned off.

## 6. Recipes

### Keep secrets out of the model

Install **Secrets Protector**. It finds API keys,
passwords, emails, phone numbers and the like in what your tool sends, swaps
each for a stand-in before the model sees it, and puts the real value back in
the reply and in tool call arguments.

For anything it doesn't recognize, mark it yourself:

```
Username: (secret: realusername) Password: (secret: realpassword)
```

To watch it work: open **Try it**, put a password in the **Table**, and ask the
model to read it or copy it to another key. `(exportit)` (below) saves the
conversation as the model saw it, stand-ins and all. More in
[Secrets Protector](tricks.md#secrets-protector).

### Unstick a refusal, or shrink a huge context

Install **Context Editor** and put it first in the channel. Open its **Live**
tab and pick a conversation. On any message:

- **Edit**: rewrite it. Change "I'm not allowed to read emails" to "Sure, happy
  to help with that" and the model carries on as if it had agreed.
- **Remove**: replace it with a short note.
- **Remove images**: drop its images, leave a note.
- **Revert**: put the original back.

Then send your next message from your tool as usual. Edits apply to that
conversation only, and a restart drops them.

### Keep long chats under the limit, automatically

Pictures and web searches pile up until a chat no longer fits any model. In
**Context Editor**'s Live tab, pick a technique under **Compaction**, below the
conversation list, and old tool output or screenshots are removed from every
request in the channel. Each option is a published technique, linked to its
source; see [Compaction](compaction.md) for what each one keeps.

### Log everything, before and after

Install **Traffic Logger** and put it first. Each request adds a line to two
files in `~/.cache/petsitter/traffic/`:

- `before.jsonl`: what your tool sent
- `after.jsonl`: the conversation after the extensions, with the model's reply

Both carry the same `request_id`:

```bash
cd ~/.cache/petsitter/traffic
jq -c '{request_id, model, last: .messages[-1].content}' before.jsonl
jq -s 'group_by(.request_id)' before.jsonl after.jsonl
```

Change the folder with its `path` setting.

### Export a conversation

With **Export It** installed (it's in Default already), type this as a message
in your tool:

```
(exportit)
```

Petsitter answers without calling the model and writes the conversation, as the
model sees it after the extensions, to `/tmp/petsitter/convo-<time>.json`.

```
(exportit: both)
```

also writes your tool's side, before the extensions: `convo-<time>-before.json`
and `convo-<time>-after.json`. Load one with `llcat -c convo.json`.

### Claude Code on Claude, Codex on OpenAI, same extensions

Put a provider after `/use/` and your tool talks to that provider with its own
key and model, through your extensions. The model on the Models page isn't used.

```bash
ANTHROPIC_BASE_URL=http://localhost:8080/use/anthropic.com claude
OPENAI_BASE_URL=http://localhost:8080/use/openai.com/v1 codex
```

Both lines are on the **Connecting** page. Any host works:
`/use/build.nvidia.com/v1`. Extensions run on chat completions and Anthropic
Messages requests; other paths are forwarded as is. Channels match as usual. See
[proxy behaviour](proxy.md#your-extensions-any-provider-use).

### Let someone try your setup over a call

By default petsitter only accepts connections from your machine. Start it so
others can reach it:

```bash
petsitter -l 0.0.0.0:8080
```

**Connecting** → **Let other people use your instance** then shows lines with
your machine's name to send them:

```bash
ANTHROPIC_BASE_URL=http://your-machine:8080/use/anthropic.com claude
OPENAI_BASE_URL=http://your-machine:8080/use/openai.com/v1 codex
```

They use their own key and provider and get your extensions. Their requests
show up in your Context Monitor and Tool Dashboard.

### Get Open WebUI identified

Open WebUI only sends `X-Title: Open WebUI` when its URL contains
`openrouter.ai`. Give it this base URL:

```
http://localhost:8080/ignore/openrouter.ai/v1
```

Petsitter drops `/ignore/<word>/`, so it's the same as `/v1`. Open WebUI then
shows up by name in Discovered programs, and **New channel for this** gives it
its own channel.

### Bypass everything

```
http://localhost:8080/bypass/v1
```

Requests to this base URL go straight to your provider: no channels, no
extensions. Only the tools pointed at it are affected.

To do the same for every tool at once, press the pause button at the top.
Everything is forwarded untouched until you press **Resume**.

## 7. Where to go next

- [What each bundled extension does](tricks.md)
- [Channels and routing](tricksets.md) (channels are "tricksets" in config and code)
- [Model configuration](models.md): more than one model, per-channel models
- [Proxy behaviour](proxy.md): `/use/`, `/ignore/`, the config diagnostic, the HTTP API
- [Compaction](compaction.md): the techniques, their sources and defaults
- [Writing your own extension](writing-tricks.md)
- [When things go wrong](troubleshooting.md)

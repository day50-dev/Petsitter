<p align="center">
<img width="480" alt="logo" src="https://github.com/user-attachments/assets/d2fd4fd3-52b3-4bb2-9c41-6623a07251a5" /><br/>
<a href=https://pypi.org/project/petsitter><img src=https://badge.fury.io/py/petsitter.svg/?2></a>
</p>

## Intro

Petsitter sits between the tools you already use and the AI provider they talk to.

That's really all it is: a small thing running on your own machine that requests
pass through on their way out, and replies pass through on their way back. Your
tools don't need to know it's there, and nothing about the way you work has to
change.

Once something is sitting in the middle, a few things become possible:

- **You can see what's actually being sent.** Every request and every reply, as
  it happens. Most of the time you'll never think about this — right up until
  something behaves oddly and you'd really like to look. *([see how](docs/tricks.md#traffic-logger))*

- **You can help a model along.** Some models are shaky at tool calling, or hand
  back JSON that doesn't quite parse. Petsitter can smooth that over in the
  middle, so you don't have to change your tools or go find a bigger model.
  *([see how](docs/tricks.md#tool-calling))*

- **You can keep private things private.** API keys and personal details can be
  taken out before anything leaves your machine. *([see how](docs/tricks.md#secrets-protector))*

None of it is on until you turn it on, and you can take it back out whenever you
like — point your tool back where it was and it's as if petsitter had never been
there.

```bash
uvx petsitter
```

Nothing else to set up. Have a look around, and if none of it is useful, no harm
done.


## A bit more precisely

**Petsitter** is an OpenAI-compatible proxy that layers smart harnesses on top of language models to give them capabilities they don't natively have. It also makes finicky behaviors reliable and dependable.

You install it, point it at a model, load a few example tricks, and suddenly things that model couldn't do before such as tool calling, structured JSON, multi-step reasoning start working. You can also protect secrets, have memory, share server instances across harnesses, and extend the tool trivially.

The built-in tricks are starting points. Tweak them, combine them, or use them as a reference to build something entirely different. Petsitter isn't a turnkey product; it's a kit.

## How It Works

<img alt="Petsitter_Intelligent_Proxy_-_Slide_2a" src="https://github.com/user-attachments/assets/b7a2a344-f438-4370-aee8-fd6f2dfe0756" />


Petsitter intercepts every request/response pair and runs it through a pipeline of hooks. Each trick picks which hooks it needs:

1. **`system_prompt`** - Inject instructions before the model sees the conversation
2. **`pre_hook`** - Modify messages or inject tool definitions before the API call
3. **`post_hook`** - Validate, retry, or transform the model's response
4. **`info`** - Declare capabilities back to your application

Tricks also have lifecycle hooks (`install`, `startup`, `shutdown`, `uninstall`) for managing resources across their lifetime.

A trick can be as simple as appending a sentence to the system prompt, or as involved as routing subtasks to three different models in parallel. There's a GUI at `/` with tabs for managing tricksets and their tricks (Tricks / Models / Agents), a live activity log (Logs), and per-trickset logging configuration (Settings).

The Tricks tab lists your local `tricks/*.py` alongside [community tricks](docs/community.md#community-tricks) published by other people, and the speech-bubble button in the header opens a [Try It panel](#try-it) that sends a message through the pipeline so you can watch which tricks fire.

You can also edit tricks, reorder them, disable, add new ones, and filter them:
<img alt="2026-07-04_15-13" src="https://github.com/user-attachments/assets/c623f29a-8724-4fdb-bc6d-a76c3022183a" />


*Petsitter* is part of the [DAY50](https://github.com/day50-dev/) suite of open-source tools for local AI workflows and constructing better agents.

The core goals of Petsitter are:
- **No model changes required** - Works with any OpenAI-compatible endpoint
- **Pluggable architecture** - Write your own tricks in Python. (Skills are included in `.agents`)
- **Transparent to your app** - Point your existing code at petsitter instead of the model
- **Mix and match** - Combine multiple tricks for compound effects

---

## Quick Start

Quickest way:

```bash
$ uvx petsitter
```

Or you can do one off invocation:
```bash
# Run petsitter, reading settings from the default config file
# (~/.config/petsitter/config.json, or $PET_CONFIG_DIR)
petsitter -l localhost:8080

# Or point at a specific config file (model, tricksets, etc. all live there)
petsitter -c another_petsitter_config.conf.json -l localhost:8080
```

Configure the upstream model, tricksets, and modelset via the dashboard at `http://localhost:8080` or the `pet` CLI — everything is persisted to the config file, so a plain `petsitter` starts the same way next time. `pet` accepts the same `-c` flag (before the subcommand, e.g. `pet -c another_petsitter_config.conf.json ls`) so both tools can target the same config area.

Either way, now you can point your AI applications to `http://localhost:8080/v1` and you're going through the petsitter middleware.

## Try It

The speech-bubble button in the header opens a conversation panel docked over the dashboard. Type a message and it goes through `chat_completions()` exactly as a real client's would: same trickset matching, same keyword gating, same hooks, same upstream. It is not a simulation.

What comes back with each reply:

- **A pill per trick.** Bright means it changed something, and the tooltip lists the stages it ran (`Ran: system_prompt, post_hook`). Dim means it was loaded but did nothing.
- **Why a trick stayed quiet.** A keyword-gated trick that didn't fire reads `Did not fire, needs keyword: banana`.
- **Timing and tokens**, next to the trickset that handled it.
- **The rows light up.** Tricks that actually did something pulse in the Loaded Tricks list, so you can watch a reorder or a config change take effect.

Drag the panel by its header to move it, drag its corner to resize, and `⇲` snaps it back to the bottom right. Whether it's open, where it sits, and the conversation itself are all remembered across refreshes.

It targets whichever trickset is selected in the pill bar, so switching tricksets switches what you're testing.


## Documentation

- **[What each bundled trick does](docs/tricks.md)** — The tricks that ship with petsitter, what each one is for, and how to turn it on.
- **[Writing your own trick](docs/writing-tricks.md)** — The four hooks, the data each one gets, and the lifecycle around them.
- **[Tricksets and routing](docs/tricksets.md)** — Grouping tricks, and deciding which requests they apply to.
- **[Connecting your coding tools](docs/agents.md)** — Pointing Claude Code, Codex and opencode at petsitter, and putting them back.
- **[Model configuration](docs/models.md)** — Naming upstream models so tricks can reach more than one.
- **[Command line reference](docs/cli.md)** — petsitter's flags and the pet subcommands.
- **[Community tricks](docs/community.md)** — Installing tricks other people wrote, and publishing your own.
- **[Proxy behaviour](docs/proxy.md)** — Host override, the config diagnostic, and the HTTP surface petsitter serves.
- **[When things go wrong](docs/troubleshooting.md)** — Known rough edges and how they show up.
- **[Working on petsitter](docs/development.md)** — Running the tests, and driving petsitter from other code.

## License

MIT

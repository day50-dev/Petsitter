# Watermarking

**Status: design, not built yet.**

Petsitter controls what leaves the machine. Some extensions now send things of
their own to the model: Expose Petsitter answers the model's tool calls itself
and asks the model again, and more will follow (notes, summaries, a model
managing its own context). Watermarking is how that internal traffic stays
inside the rules without each extension having to get it right.

## The problem

Two things go wrong when an extension sends its own content to the model.

- **It skips the egress controls.** The conversation an extension sends back
  has been through the channel's `pre_hooks`, but what the extension adds
  afterwards hasn't. Expose Petsitter's tool answers go to the provider without
  Secrets Protector ever seeing them. Today they hold only extension names and
  settings (a few of them local file paths), but the gap is real.
- **Sending it through the controls invites recursion.** Run petsitter's own
  content back through the pipeline and every extension sees it again,
  including the one that produced it. Without a way to tell its own output from
  the user's, an extension can trigger itself, or two extensions can trigger
  each other forever.

## The rules

1. **The model sees only what has passed the egress controls.** Never the
   request as it was before them: a model that can ask what the request looked
   like before Secrets Protector has Secrets Protector switched off.
2. **Everything petsitter adds goes through the egress controls,** the same as
   the user's messages.
3. **Controls such as Secrets Protector are the user's,** unless the user
   grants sudo (Expose Petsitter's **Let the model change settings**). Without
   it, a model can change nothing. With it, the model can change anything,
   controls included, by the user's explicit choice: it's a footgun on purpose.
   Either way the model works from what's on and what it can do, never from a
   view of the request before the controls (rule 1).
4. **The guarantees live in the parent,** never in the extensions.

## The mark

Each time petsitter sends something of its own back through the pipeline, that
pass gets a fresh mark, a reserved name (`reserved("petsitter_internal")`):
petsitter's id, the context, then a 22-character base62 id (128 bits).

```
gRefWg2D7zO8-petsitter_internal-7hQ2mZx9Lc4TfR0bWn5Kd1
```

The mark identifies a pass, not an extension. What an extension needs to know
is "have I already acted on this?", and a unique ID answers that exactly. It
also handles nesting: a pass started inside another pass gets its own ID.

It goes in the text itself, not in a field on the message: extensions rebuild
messages and copy text around, and a field is lost when they do. It can't be
confused with the id's other uses: each use has its own context, like
Secrets Protector's stand-ins (`gRefWg2D7zO8-sp-<id>`), and
petsitter's tool names have no id (`gRefWg2D7zO8-<name>`).

## A pass, start to finish

1. **An extension asks the parent to start a pass.** Extensions never talk to
   each other or to a model directly; every internal call goes up to the
   framework (`call_upstream_sync` and its successors). The parent mints the
   ID, tags the content, and records in the request's metadata which extension
   started it. The record lives with the request, not on the extension, so
   concurrent requests can't mix them up.
2. **The content goes through the channel's `pre_hooks`,** minus the extension
   that started the pass. `post_hooks` are never rerun for a pass; that's where
   a loop would come from. Each extension sniffs for marks and skips a pass it
   started, or one descended from it. Egress controls don't skip: Secrets
   Protector masks marked content like anything else, which is the point.
3. **Just before bytes leave, the parent strips every mark,** on the request
   to the provider and on the reply to your tool. Stand-ins and tool names stay.

## Lineage

A pass carries the IDs of every pass it came from. Without that, two extensions
can ping-pong: A starts pass 1, B reacts and starts pass 2, A sees an ID it
didn't start and reacts again, each time with a fresh ID. With lineage, A finds
its own pass in pass 2's ancestry and stops, so a cycle can't form.

Extensions don't carry lineage themselves. Every pass starts through the
parent, so the parent keeps a stack of active passes for the request, and a
pass started while another is running gets the whole stack.

## The bulkhead

The parent never depends on the marks for its guarantees. They're a courtesy
so extensions can recognise passes; the rules are enforced from the parent's
own record, which no extension can touch.

- **Cycles are refused at the source.** An extension that already started a
  pass in the current lineage can't start another, whether or not its mark
  survived in the text.
- **Depth is capped** (say 8 nested passes per request), for long chains of
  distinct extensions.
- **There's a budget** of internal model calls per request: no fork bomb also
  means no surprise bill.
- **Stripped marks are caught.** The parent knows which marks it handed out. If
  one is missing after the hooks run, an extension removed it, and that
  extension gets a "!" in the dashboard like any other problem.
- **Stripping before egress is the parent's job,** so a mark can't leak to the
  provider or your tool.

A buggy extension, or a model producing internal traffic in some unforeseen
way, can break only its own feature: it loses the ability to recognise its own
passes. It can't loop the request, run up the budget, or get past the egress
controls. Any combination of extensions and models can produce internal
traffic as it pleases and it still ends.

The test for anything new in the framework: does the guarantee hold if every
extension misbehaves? If it relies on extensions behaving, it belongs in the
parent.

## Primitives

Next to `get_prefix()`, `reserved()` and `reserved_pattern()` in
`petsitter.trick`:

- start a pass: returns its ID, or refuses (cycle, depth, budget)
- find the pass IDs in a piece of text
- strip marks from text
- whether a pass (or its lineage) was started by the calling extension

An extension's whole contract: start passes through the parent, and skip marked
content whose lineage includes a pass it started.

## Building it

Mostly bookkeeping, a couple of hundred lines plus tests:

- the pass stack and counters in the request's metadata
- the primitives above
- `call_upstream_sync` running the channel's `pre_hooks` (minus the caller)
  before sending
- stripping at each point a request leaves, on both APIs
- stripping in replies; in a streamed reply a mark can be split across chunks,
  which the reply window already handles for Secrets Protector's stand-ins (a
  window as long as a mark)
- comparing marks handed out against marks present, and reporting the
  extension that dropped one

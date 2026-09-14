# Persistent Scratchpad — Design Specification

## 1. Purpose

Build a small local scratchpad application for Linux, initially targeting Manjaro.

The application is intentionally **not a note-taking system**. It is temporary working memory for short-lived information that should normally be processed and removed later.

Typical contents include:

- temporary thoughts
- reminders
- bullet-point working notes
- copied text
- URLs
- serial numbers and identifiers
- terminal/log output
- screenshots
- arbitrary temporary files

At the end of a normal day, the current scratchpad may be completely empty because anything worth retaining should have been moved into a separate structured note-taking system.

Despite this ephemeral workflow, the scratchpad must be extremely resistant to accidental data loss and should retain a complete history of previous states.

---

# 2. Core design principles

## 2.1 Plain text remains plain text

The main scratchpad is fundamentally a normal text buffer.

Avoid:

- rich text
- structured block models
- hidden object nodes
- automatic semantic interpretation
- automatic list management
- editor-generated formatting structures
- invisible attachment markup
- inlay replacement of stored text

What the user sees in the editor should correspond directly to the stored text.

If the document contains:

```text
- investigate CAN timeout
  Ab3Fmx7QK9v2Rt8Nc4WpLd
```

that exact text should be stored.

Attachments are recognized because the alphanumeric token happens to validate, not because the surrounding document has special syntax.

---

## 2.2 The editor should be "dumb" but capable

Editing should feel more like a source-code editor than a structured note application.

Plain text operations must always behave predictably.

Optional commands may make common structures convenient, for example:

- indent selected/current lines
- outdent
- move line or logical bullet/block up/down
- delete/cut current bullet/block
- continue indentation on newline
- optionally continue a `- ` bullet on newline
- insert plain newline without continuation

These are editor commands only.

For example:

```text
- First thing
  - Subordinate thought
  - Another thought
- Unrelated thing
```

remains literal text containing `-`, spaces, and newlines.

The application must not internally transform this into list objects.

Automatic "helper" functionality should be conservative and easy to bypass.

---

# 3. Main interaction model

The scratchpad should ideally behave like a drop-down terminal for text.

A global shortcut should toggle it:

```text
global shortcut
→ scratchpad appears
→ typing focus is immediately in the editor

same shortcut
→ scratchpad disappears
```

Desired window behavior:

- fast appearance
- current workspace
- optionally floating/always-on-top while visible
- immediate keyboard focus
- persistent buffer between invocations
- no filename prompts
- no save dialogs

The current scratchpad should never be an unnamed/untitled editor buffer.

---

# 4. Saving and crash persistence

Every document mutation should become durable automatically.

There should be no user-facing "Save" concept during normal operation.

Important distinction:

- logical history captures every document mutation
- physical synchronization to persistent storage can be batched briefly for efficiency

Example:

```text
keystroke/edit
→ immediately recorded in application state/history
→ append to storage
→ fsync on a short interval
```

An application crash should normally lose nothing once the edit has reached the OS.

A power failure may lose only the very small interval since the latest durable flush.

A target flush interval around hundreds of milliseconds to roughly one second is reasonable and should be configurable if useful.

The persistent storage format must tolerate a torn/incomplete final write.

---

# 5. Exact history model

## 5.1 Record every document mutation

Do not decide whether an edit is "significant enough" to snapshot.

Every actual mutation of the document should be represented in history.

Do **not** log raw keyboard events.

The correct abstraction is document operations such as:

```text
INSERT position=153 text="h"
INSERT position=154 text="e"
DELETE position=155 length=3
REPLACE range=...
```

This naturally handles:

- individual typed characters
- paste
- deletion
- mouse-based editing
- cut
- undo
- redo
- programmatic editor commands
- IME/input-method operations

If a user types:

```text
hello there
```

character by character, history should reproduce the intermediate states character by character.

Do not coalesce those events merely because they happen close together.

A pasted string, however, is naturally one edit operation because that is how it entered the document.

---

## 5.2 History represents what actually existed

Undo and redo do not alter history.

They create new document mutations.

Example:

```text
12:01:01 INSERT "foobar"
12:01:04 DELETE "foobar"    # user pressed Undo
```

When replaying 12:01:02, `foobar` must be visible.

History is therefore an immutable record of what the scratchpad actually contained over time, rather than an editor undo tree.

---

# 6. Timeline

The history UI should primarily be temporal rather than commit-based.

The user should be able to select essentially **any point in time at which the device/application was running** and see the scratchpad as it was at that moment.

Conceptually:

```text
08:13 ━━━━━━━━━━━━━━━ 12:04    13:01 ━━━━━━━━━━━━━━━━ 18:22
       powered on                       powered on
```

Selecting 10:37 reconstructs the most recent state at or before 10:37.

Periods when the system/application was not running should be visually distinguishable.

The history system should therefore record lifecycle/session events in addition to text mutations, for example:

```text
SYSTEM_START
DOCUMENT_EDIT
DOCUMENT_EDIT
...
SYSTEM_STOP
SYSTEM_START
...
```

Use monotonic time for ordering/deltas, with sufficient wall-clock anchors to map events accurately to real timestamps.

The timeline may optionally visualize edit activity density.

---

# 7. Checkpoints

Periodic full-state checkpoints are useful for performance.

They are **not semantic snapshots** and must not determine what history exists.

Example:

```text
checkpoint
↓
10,000 edit events
↓
checkpoint
↓
...
```

To reconstruct a point in time:

1. locate the newest checkpoint before that time
2. load it
3. replay subsequent events until the desired timestamp

Checkpoint frequency should be chosen solely based on performance/storage tradeoffs.

The exact mutation history remains authoritative.

---

# 8. Storage model

The implementation should investigate a compact append-only binary event log rather than naïvely using one SQLite row per typed character.

The common event should be cheap.

A conceptual representation might be:

```text
[time_delta]
[operation + flags]
[position or position_delta]
[payload]
```

Use compact integer encoding such as varints where appropriate.

For ordinary single-character typing, the storage overhead should ideally be only a handful of bytes beyond the character itself.

Requirements:

- append-only normal operation
- precise event ordering
- crash-safe detection of incomplete tail records
- versionable file format
- periodic checkpoints
- indexing sufficient for fast time-based seeking
- robust validation of persisted data

SQLite may still be used for metadata/indexing if useful, but one SQL row per character is probably unnecessary overhead.

---

# 9. Current document

The application should maintain an easily recoverable current scratchpad state in addition to event history.

Whether this is:

- reconstructed from the event log,
- stored as a current-state file,
- stored through checkpoints,
- or some combination

is an implementation decision.

The important behavioral requirement is:

> startup after a normal or abnormal shutdown should quickly restore the latest durable document state.

---

# 10. Attachments

Large/non-textual objects should live outside the main text buffer.

Examples:

- screenshots
- images
- very large copied logs
- arbitrary files
- possibly other media later

The scratchpad contains only a compact textual token referencing the attachment.

Attachments should preferably be immutable once created.

Content-addressed internal blob storage may be useful, especially for deduplication and history preservation, but this is not required by the external token format.

---

# 11. Large text and logs

Large pasted text should optionally be stored as an attachment rather than inserted into the main buffer.

Example workflow:

```text
Paste normally
→ inline text

Paste as attachment
→ attachment blob + token inserted into scratchpad
```

There should be explicit user intent rather than always relying on heuristics.

A dedicated shortcut such as:

```text
Ctrl+V          Paste normally
Ctrl+Shift+V    Paste as attachment
```

is a possible mapping, but final bindings are implementation/UI decisions.

Automatic large-paste detection should act only as a guardrail.

Example:

```text
User uses ordinary Paste on 50,000 lines

"This is a very large inline paste."
[Paste inline anyway]
[Store as attachment]
```

Explicit "Paste as attachment" should not ask this question.

Useful deterministic metadata for previews/details may include:

For text:

- creation/import timestamp
- number of lines
- number of characters
- encoding where relevant

For images:

- timestamp
- dimensions
- MIME/type

File size may be available in details but is not especially useful for identifying images/logs and need not be prominently displayed.

Do not require user-defined attachment names.

Context belongs naturally in ordinary scratchpad text surrounding the token.

---

# 12. CLI integration

Provide a command-line integration suitable for piping large output directly into the scratchpad attachment store.

For example:

```bash
journalctl -b | scratchpad-attach
```

This should:

1. consume stdin
2. create an immutable text attachment
3. insert its token into the currently active scratchpad, ideally at the cursor if practical

Also consider a direct text insertion command:

```bash
echo "temporary reminder" | scratchpad-insert
```

Possible future extension:

```bash
idf.py monitor | scratchpad-attach
```

Live-stream attachment capture is not required initially; waiting for EOF is acceptable for the first implementation.

---

# 13. Clipboard integration

Clipboard handling should distinguish explicit intent.

At minimum:

### Normal paste

For textual clipboard content:

```text
Paste
→ insert directly into text
```

For image clipboard content, direct attachment creation is reasonable because there is no plain-text representation to insert.

### Paste as attachment

For textual clipboard content:

```text
Paste as attachment
→ create text attachment
→ insert attachment token
```

For image content:

```text
Paste as attachment
→ create image attachment
→ insert token
```

A separate global action may optionally:

```text
open/toggle scratchpad
→ immediately paste current clipboard
```

This would be useful for fast capture.

---

# 14. Screenshot workflow

A screenshot capture action should be able to:

1. capture/select a screen region
2. store the image as an attachment
3. insert its token at the current scratchpad position
4. optionally bring up the scratchpad for immediate annotation

The attachment itself stays outside the text document.

---

# 15. Attachment token design

The document should not contain syntax such as:

```text
[[attachment:abc123]]
```

Instead, an attachment reference should be an ordinary-looking fixed-length alphanumeric token.

Allowed alphabet:

```text
0-9
a-z
A-Z
```

No punctuation or special prefix is required.

Example visual form:

```text
7Fq9Lc2mwKa4Pz1TR8xNVe
```

The exact final length should be confirmed during implementation design, but approximately 22 Base62 characters is currently favored.

---

# 16. Token semantics

The token must provide three distinguishable states:

### 1. Ordinary text / never issued locally

The candidate does not authenticate.

Treat it as ordinary text.

### 2. Valid locally issued token + object exists

Resolve and preview the attachment.

### 3. Valid locally issued token + object missing/corrupt

The application can reliably report:

```text
Valid local attachment reference,
but backing object is unavailable/corrupt.
```

This distinction is important.

---

# 17. Instance-specific token authentication

Each application installation should have a secret local key.

A token should contain internally something conceptually equivalent to:

```text
object identifier
+
domain/type information if required
+
authentication tag
```

The authentication tag should be derived from a keyed cryptographic primitive such as:

- HMAC-SHA-256 truncated appropriately
- keyed BLAKE3
- another well-established keyed MAC/hash

A 64-bit authentication tag is a reasonable target for this non-adversarial application.

This means a token copied from another installation will normally fail validation and be treated as ordinary text.

A valid local token whose backing object is missing therefore strongly indicates damage/missing storage rather than random pasted text.

---

# 18. Token IDs and visual randomness

Internally, object identifiers may be monotonically increasing integers because that makes uniqueness/database handling simple.

However, **the sequential structure must not be externally visible**.

Do not simply Base62-encode:

```text
counter || MAC
```

if that produces visually shared prefixes/suffixes.

Two sequential attachment IDs should yield visually unrelated tokens.

Conceptual requirement:

```text
object ID 437 → 7Fq9Lc2mwKa4Pz1TR8xNVe
object ID 438 → B3vX0sRq6Jt9hMa5D2uKzc
```

Use a keyed reversible scrambling/permutation or equivalent fixed-width transformation before Base62 representation.

Conceptually:

```text
internal payload:
[counter | domain | authentication]

↓ keyed reversible diffusion/permutation

external token:
opaque pseudorandom-looking Base62
```

Parsing reverses this operation, validates the authentication tag, and extracts the object ID.

The precise cryptographic construction must be reviewed during implementation design rather than invented ad hoc.

---

# 19. Token length

Base62 carries approximately:

```text
log2(62) ≈ 5.954 bits/character
```

Approximate capacities:

```text
16 chars ≈ 95 bits
18 chars ≈ 107 bits
20 chars ≈ 119 bits
22 chars ≈ 131 bits
```

A currently attractive design is approximately:

```text
64-bit object ID
64-bit authentication tag
--------------------------
128 bits total
```

which requires 22 Base62 characters.

This gives extremely low accidental validation probability while staying much shorter than a conventional textual UUID.

The implementation-design phase should verify the exact encoding and whether all 128-bit values are represented canonically.

---

# 20. Candidate token recognition

Do not search every substring of arbitrary text.

A token candidate should be a **maximal contiguous run of `[0-9A-Za-z]` of exactly the configured token length**.

For example:

```text
foo 7Fq9Lc2mwKa4Pz1TR8xNVe bar
    └──── candidate ──────┘
```

But:

```text
abc7Fq9Lc2mwKa4Pz1TR8xNVedef
```

is one longer alphanumeric run and therefore is not interpreted as an attachment.

This reduces accidental candidate generation in:

- hashes
- encoded data
- logs
- arbitrary pasted strings

without introducing visible syntax.

---

# 21. Token domains

The internal authenticated payload should probably include a domain/version identifier such as conceptually:

```text
"attachment-v1"
```

This allows the same cryptographic machinery to be extended safely to other tokenized object classes later.

Do not expose the domain/type visually in the token unless an actual future use case demonstrates that doing so is valuable.

Opaque tokens are currently preferred.

---

# 22. Attachment preview UI

Do not replace tokens inline with pretty labels.

The stored text and rendered editor text should remain identical.

Instead, provide **two persistent preview areas**:

### Hover preview

Always represents whatever attachment token is currently under the mouse pointer.

If there is no valid attachment under the pointer, the preview area remains present but blank.

### Cursor preview

Always represents the valid attachment token at/around the current text cursor.

If there is none, the preview remains present but blank.

The preview pane behavior depends on attachment type.

### Image

Show the image.

### Text/log

Show a text viewer with useful functionality such as:

- scrolling
- line numbers
- search
- possibly lightweight syntax/log highlighting
- potentially first/last-line navigation

### Arbitrary file

Show available metadata and actions such as opening externally.

The main editor remains untouched.

---

# 23. Attachment history

Deleting an attachment token from the current scratchpad does **not** normally delete the backing attachment.

Historical document states may still reference it.

The attachment therefore remains available as long as retained history requires it.

Attachment garbage collection may remove blobs only once no retained historical state references them.

---

# 24. Redaction / purge

Normal deletion preserves history.

There must also be an explicit destructive operation for cases such as accidentally pasting:

- API keys
- passwords
- private tokens
- confidential content
- unwanted screenshots

This should be exposed through a discoverable menu and/or command palette rather than relying on a rarely remembered keyboard shortcut.

Possible operations:

```text
Redact this historical occurrence
Redact this exact content everywhere
Purge this attachment from history
```

Exact wording and UX should be designed carefully.

Destructive redaction should require confirmation and clearly communicate that it rewrites scratchpad history.

---

# 25. Purge implementation

The normal event log is append-only, but purge/redaction is a deliberate exception.

Do not attempt unsafe in-place mutation of binary log records.

Instead use a history-rewrite process:

```text
old event history
↓
replay/filter/redact
↓
write new history
↓
validate
↓
fsync
↓
atomic replacement
```

Affected checkpoints must also be regenerated or removed.

For attachments:

1. remove/redact historical references as requested
2. rewrite relevant history/checkpoints
3. delete the backing blob only once nothing retained references it

---

# 26. Scope of deletion guarantees

The application must not claim forensic secure deletion.

Crash persistence fundamentally results in multiple durable copies over time.

Deleted material may additionally survive in:

- filesystem snapshots
- backups
- SSD flash translation layers
- swap
- crash dumps
- stale filesystem blocks

The appropriate product language is:

```text
Remove permanently from scratchpad history
```

not:

```text
Securely erase from physical storage
```

Cryptographic erasure could be investigated later if stronger deletion properties are desired, but it is not necessary for the first implementation.

---

# 27. Menus and command discovery

The application should have a minimal conventional menu UI because infrequent functionality must remain discoverable.

Possible high-level structure:

```text
File
  Clear scratchpad
  Export current state

Edit
  Undo
  Redo
  Indent
  Outdent
  Move block up
  Move block down

Insert
  Paste inline
  Paste as attachment
  Attach file
  Attach clipboard image

History
  Show timeline
  Compare with current
  Restore/copy historical state
  Redact selection from history
  Purge attachment from history

View
  Hover preview
  Cursor preview
  Timeline
```

The exact menu contents are not prescriptive.

---

# 28. Command palette

A searchable command palette is strongly desirable.

For example:

```text
Ctrl+Shift+P

> redact
History: Redact selection from history

> attach
Insert: Paste as attachment
Insert: Attach file
```

This provides discoverability without cluttering the main interface.

Frequent actions can expose keyboard shortcuts in menus/palette entries so shortcuts can be learned naturally over time.

---

# 29. Interaction philosophy

Use this hierarchy:

### Frequent operation
Direct keyboard shortcut.

### Operation where user intent matters
Provide separate explicit verbs.

Example:

```text
Paste
Paste as attachment
```

Do not force the software to infer intent.

### Rare operation
Menu + command palette.

### Destructive operation
Menu/command palette + explicit confirmation.

### Automatic inference
Use only as a safety mechanism or convenience fallback, not as the primary interaction model.

The application should generally avoid trying to be clever.

---

# 30. Large-paste handling philosophy

Explicit user intent is preferred.

Example:

```text
normal paste
→ inline

paste-as-attachment command
→ attachment
```

If an ordinary paste exceeds a configurable safety threshold, the application may warn:

```text
Large paste: 18,431 lines

[Insert inline]
[Store as attachment]
```

This threshold is a protection against accidental scratchpad bloat, not a semantic classifier.

---

# 31. No automatic daily rollover

Do not automatically empty the scratchpad at midnight.

A scratchpad item left over from Friday may intentionally still need attention on Monday.

The user explicitly clears/removes content when it has been processed.

History naturally records the transition to an empty document.

---

# 32. Clearing the scratchpad

"Clear scratchpad" means:

```text
current document → empty
```

This is just another historical mutation.

Old contents remain accessible through history unless explicitly redacted/purged.

The empty state itself is meaningful and should appear in history.

---

# 33. History browsing vs editing

Historical views should not accidentally mutate historical state.

A sensible model is:

```text
normal mode
→ edits current document

history mode
→ read-only reconstructed state
```

Possible explicit operations from history include:

- copy text
- copy attachment token
- compare to current
- restore selected text
- restore entire historical state into current buffer
- redact historical content

The exact restoration UX should be fleshed out during implementation design.

---

# 34. LLM/AI features

No LLM should be required for core functionality.

Attachment identity and labels should remain deterministic.

Potential future optional actions:

```text
Summarize attachment
Explain log
Extract errors
Describe screenshot
Extract screenshot text
```

Any AI-generated result should be explicitly requested and treated as derived information, not canonical attachment metadata.

The application must remain fully useful without AI.

---

# 35. Non-goals

Do not turn this into:

- a knowledge base
- a task manager
- a wiki
- a Logseq/Notion/Obsidian replacement
- an outliner data model
- a structured notebook
- a document management system
- an automatic categorization system

Avoid unnecessary features such as:

- tags
- notebooks
- pages
- backlink graphs
- properties
- databases exposed to the user
- automatic semantic names
- checkboxes as a core workflow

The value of the application is precisely that information can enter and leave it with almost zero organizational overhead.

---

# 36. Desired conceptual architecture

A likely architecture is:

```text
                        ┌──────────────────┐
                        │ Plain-text editor │
                        └─────────┬────────┘
                                  │
                         document mutations
                                  │
                                  ▼
                        ┌──────────────────┐
                        │ Document model   │
                        └─────────┬────────┘
                                  │
                   every mutation│
                                  ▼
                    ┌────────────────────────┐
                    │ Append-only event log  │
                    └───────────┬────────────┘
                                │
                     periodic checkpoints
                                │
                                ▼
                       ┌─────────────────┐
                       │ Checkpoint store│
                       └─────────────────┘


Attachment creation
        │
        ▼
┌───────────────────┐
│ Attachment store  │
└─────────┬─────────┘
          │
 authenticated opaque token
          │
          ▼
   inserted as plain text


Event log + checkpoints
          │
          ▼
     history engine
          │
          ▼
       timeline
```

The UI remains a relatively thin layer over this model.

---

# 37. Important implementation questions for the next agent

The next design/implementation phase should specifically resolve:

1. Which Linux GUI toolkit/framework best fits Manjaro and global shortcut requirements.
2. Whether Wayland/COSMIC/KDE/GNOME differences affect global toggle/window behavior.
3. Exact text-widget behavior and how document mutations are intercepted reliably.
4. Exact binary event-log format.
5. Crash consistency and fsync strategy.
6. Monotonic/wall-clock timestamp representation.
7. Checkpoint cadence and compression.
8. Timeline indexing/replay algorithm.
9. Exact attachment-store layout.
10. Whether attachment blobs should be content-addressed.
11. How attachment reference counts across history are determined efficiently.
12. Exact token construction.
13. Correct standard cryptographic primitive for keyed reversible scrambling/permutation.
14. Exact token MAC construction and bit allocation.
15. Base62 canonical encoding rules.
16. Secure storage and backup behavior for the installation secret.
17. Behavior when the installation secret is lost but attachment data remains.
18. Migration/export/import of the entire scratchpad instance.
19. CLI/GUI IPC protocol used by `scratchpad-attach` and `scratchpad-insert`.
20. Clipboard MIME handling.
21. Screenshot-tool integration under Wayland.
22. Text/log preview implementation.
23. Redaction-history rewrite algorithm.
24. Recovery behavior after interrupted redaction.
25. History retention and optional garbage collection.
26. Testing strategy for crash consistency and corrupted history.
27. File-format versioning/migration strategy.
28. Whether multiple simultaneously running application instances are supported or prohibited.

---

# 38. Priority order for an initial implementation

A sensible implementation sequence is:

### Phase 1 — core text scratchpad

- one persistent plain-text document
- minimal editor
- global show/hide
- automatic persistence
- exact edit-event logging
- crash recovery

### Phase 2 — history

- checkpoints
- timeline
- arbitrary historical reconstruction
- session/power-on intervals

### Phase 3 — attachments

- attachment storage
- authenticated fixed-length Base62 tokens
- cursor preview
- hover preview
- clipboard image attachment
- text/log attachment

### Phase 4 — capture workflows

- Paste as attachment
- large-paste guardrail
- `scratchpad-attach`
- `scratchpad-insert`
- screenshot capture integration

### Phase 5 — destructive history management

- redact occurrence
- redact exact content
- purge attachment
- crash-safe history rewrite
- attachment garbage collection

### Phase 6 — polish

- command palette
- configurable shortcuts
- editor block/bullet commands
- history comparison
- optional export/import
- optional retention policies

---

# 39. Core acceptance criteria

The first mature version should satisfy the following:

1. Typing a character and suffering an application crash shortly afterward normally does not lose that character.
2. Every document mutation can be replayed chronologically.
3. Typing `hello` character-by-character produces historical states for `h`, `he`, `hel`, `hell`, and `hello`.
4. Pasting `hello` produces one paste mutation.
5. Undoing an edit leaves the original state visible at earlier historical timestamps.
6. The user can select arbitrary times on the timeline and reconstruct the corresponding document.
7. Non-running intervals are identifiable on the timeline.
8. The main document is always ordinary plain text.
9. Attachments are represented only by ordinary fixed-length alphanumeric tokens in that text.
10. Arbitrary pasted alphanumeric strings almost certainly do not validate as local tokens.
11. Tokens issued by another installation do not normally validate.
12. A valid local token with missing backing data can be distinguished from random ordinary text.
13. Sequentially created attachments receive visually unrelated tokens.
14. Hovering a valid attachment token updates the hover-preview pane without modifying editor contents.
15. Moving the cursor onto a valid token updates the cursor-preview pane.
16. Large logs can be stored externally and represented by a token instead of bloating the editor.
17. CLI output can be piped into an attachment.
18. Normal deletion preserves historical content.
19. Explicit redaction can remove unwanted content from retained scratchpad history.
20. The application never promises forensic secure deletion.
21. Clearing the current scratchpad does not destroy history.
22. No structured-note workflow is required to use the application effectively.

---

# 40. Product summary

The desired application can be summarized as:

> A single, always-persistent plain-text scratchpad with exact temporal history and lightweight external attachments.

Its defining characteristics are:

```text
plain text
+
zero-save workflow
+
every-edit event history
+
continuous timeline
+
opaque authenticated attachment tokens
+
two passive preview panes
+
explicit capture commands
+
optional destructive redaction
```

The application should optimize for **predictability, immediacy, and lack of organizational friction**.

It should feel much closer to an unusually durable text editor buffer than to a conventional note-taking application.
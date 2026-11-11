# Pancho, the coding dog 🐾

**English** · [Español](README.es.md)

**A floating macOS pet that tells you when Claude Code or Codex need something from you, and lets you answer without hunting for the window.**

<p align="center">
  <img src="media/pancho-demo-en.gif" alt="Pancho demo" width="420">
</p>

<p align="center">
  Open source · macOS · Claude Code + Codex · no accounts, no keys, no API
</p>

> Pancho's own windows and buttons are in Spanish for now. This guide quotes each label as it appears on screen, with its meaning next to it.

---

## The problem

You work with several AI agents at once: three Claude Code conversations, a couple of Codex ones… Sooner or later each of them stops to ask you something or to request permission. And you don't notice: the window is behind another one, on another desktop or in full screen. The agent waits half an hour for a two-second answer.

## What Pancho does

Pancho lives in a corner of your screen, above everything else (full-screen apps included).

- **It tells you right away.** When an agent asks you a question or requests permission, Pancho perks up and a card appears with the project and the conversation that needs you.
- **You answer from there.** Click the card, pick an option or type your answer, and press «Enviar» (Send). The agent carries on in its original conversation, as if you had answered in its own window.
- **All your conversations at a glance.** One card per running conversation, with its status: working, waiting for you, or done.
- **Several at once, never mixed up.** Each answer goes back to exactly the conversation that asked.
- **Nothing gets lost.** If you close the card or don't answer, the question goes back to its app and you answer it there. Pancho never answers for you and never resends anything on its own.

### What you can answer from Pancho

| | Claude Code | Codex |
|---|:---:|:---:|
| Agent questions (with options or free text) | ✅ | ✅ |
| Permissions (Allow once / Always allow / Deny) | ✅ | Answered in Codex |
| The "next step" box of the Claude app | ✅ | — |
| List of running conversations | ✅ | ✅ |

## How it works (and why it asks for no keys)

Pancho **uses no API and no account**. It doesn't talk to Anthropic's or OpenAI's servers, and it opens no network connections.

It relies on the official *hooks* of Claude Code and Codex: small notifications those apps fire on your own Mac when something happens (a conversation starts, the agent asks a question, requests a permission, finishes a turn). When you press «Conectar» (Connect), Pancho subscribes to those notifications. That's all.

```
Claude Code / Codex  ──(local hook)──▶  Pancho bridge (Python)  ──▶  private files on your Mac  ──▶  Pancho.app
        ▲                                                                                              │
        └────────────────────────────── your answer ───────────────────────────────────────────────────┘
```

- **App**: Swift + AppKit, no dependencies.
- **Bridge**: Python, standard library only.
- **Data**: in `~/Library/Application Support/Pancho/`, only on your Mac.

## Requirements

- macOS 13 (Ventura) or later.
- Apple's developer tools (free; they include Swift and Python 3). If you don't have them:

  ```bash
  xcode-select --install
  ```

- Claude Code and/or Codex already installed and signed in, the way you normally use them.

## Installation

There are two ways. Both do the same thing: download Pancho into `~/pancho`, build it on your Mac, place it in `~/Applications/Pancho.app` and open it. No password is requested and no background services are installed. Because the app is built on your own machine, macOS doesn't show the "unidentified developer" warning.

### Option A: one line in Terminal

Open the **Terminal** app, paste this and press Return:

```bash
git clone https://github.com/javierprietoprogram/pancho-the-coding-dog.git ~/pancho && ~/pancho/install.sh
```

If it says Apple's tools are missing, run `xcode-select --install`, wait for the installation to finish and repeat the line above.

### Option B: let your agent install it

If you'd rather not open Terminal, paste this text into a conversation in the **Claude desktop app (Code tab)** or the **Codex desktop app**. It has to be the desktop app, on your Mac: it doesn't work on the web or in a cloud session, because Pancho is installed on your machine.

```text
Install Pancho on this Mac. It is an open-source floating pet that tells me when
Claude Code or Codex need my answer.
Repository: https://github.com/javierprietoprogram/pancho-the-coding-dog

Do this, in this order:
1. Check that this is macOS 13 or later and that `xcrun --find swiftc` works.
   If it fails, tell me to run `xcode-select --install` and stop there.
2. If the folder ~/pancho does not exist, clone the repository into it. If it
   already exists, go into it and run `git pull`.
3. Read install.sh and explain to me in one sentence what it does before running it.
4. Run `~/pancho/install.sh`. It builds the app, copies it to ~/Applications/Pancho.app
   and opens it. If your environment blocks it, ask me for permission to run it
   outside the sandbox.
5. Check that ~/Applications/Pancho.app exists and that Pancho is open.

Do not touch ~/.claude or ~/.codex and do not run pancho_install.py: I do the
connection myself from Pancho's window. When you finish, tell me only this:
- to press «Conectar» (Connect) for Claude and/or Codex in Pancho's window;
- that for Codex I also have to approve Pancho's hooks in "Review hooks";
- to open a new conversation so it starts working.
```

### Updating

Pancho will keep getting improvements. To get the latest version:

```bash
cd ~/pancho && git pull && ./install.sh
```

You don't need to connect Claude or Codex again.

## Connecting Claude and Codex

The first time it opens, Pancho shows its welcome window («Hola, soy Pancho»). If you close it, you can always get it back from Pancho's icon in the menu bar → **Configuración…** (Settings).

### Claude Code: one click

1. Press **Conectar** (Connect) in the Claude row.
2. Open a **new** Claude Code conversation (the ones already open won't notice until you restart them).

That's it. From then on, Claude's questions and permission requests show up in Pancho.

### Codex: one click and your approval

For safety, Codex doesn't let any app enable hooks unless you approve them inside Codex. Pancho walks you through it with a step-by-step window:

1. Press **Conectar** (Connect) in the Codex row. The guide opens.
2. Open Codex (the guide has an «Abrir Codex» button).
3. Press **Review hooks** (it appears next to the box where you type; if you don't see it, go to *Settings > Hooks*).
4. Approve Pancho's entries: they are the ones ending in `pancho_hook.py … --provider codex`.
5. Type anything in a new chat. As soon as Codex notifies Pancho, the guide shows **✓ Listo** (Done).

You only do this once. You can see the steps again with the «Ver los pasos de Codex…» button.

## Daily use

- **Move Pancho**: drag it wherever you like; the cards arrange themselves around it.
- **Answer**: click the card of the conversation that's waiting for you, answer and press «Enviar» (Send).
- **Remove a conversation from the list**: with the × on its card. It comes back by itself if it asks you a new question.
- **Menu bar menu**: open the next pending question, refresh conversations, show or hide the list, turn animations on or off, change Pancho's size, and quit.

### Settings («Configuración…»)

| Setting (label in the app) | What it does |
|---|---|
| Open Pancho at login («Abrir Pancho al iniciar sesión») | Optional, off by default. |
| Open questions as soon as they arrive («Abrir las preguntas en cuanto lleguen») | If off, Pancho only notifies you and the question opens when you click its card. |
| Animations («Animaciones de Pancho») | Turn off to keep Pancho still. |
| Size («Tamaño de Pancho») | Small, Normal or Large. |
| Wait for the answer («Esperar la respuesta») | 5, 15 or 30 minutes. After that time without an answer in Pancho, the question goes back to its app. |

## Privacy and security

- **No network, no accounts, no keys.** Pancho sends nothing outside your Mac.
- **It doesn't read your history.** It doesn't open your conversation transcripts; it only receives the specific notification for each question or permission.
- **It stores the minimum.** The text of the questions it handles and your answers, in a private folder of your user account.
- **It respects your configuration.** When connecting, it only adds its own entries to `~/.claude/settings.json` and `~/.codex/hooks.json`, and it saves a backup of the file first. Your hooks and settings are left alone.
- **If Pancho is closed, nothing breaks.** Prompts appear in Claude and Codex as usual.
- **It doesn't click or type in other apps.** It needs no Accessibility or Screen Recording permissions.

A performance note: with Claude connected, a very short Python process runs after each tool the agent uses (that's how Pancho learns you already granted a permission in the Claude app itself).

## Disconnecting and uninstalling

**Disconnect**: in «Configuración…», press **Desconectar** (Disconnect) for Claude or Codex. Only the entries Pancho added are removed; the rest of your configuration stays the same.

**Uninstall completely** (after disconnecting):

```bash
rm -rf ~/Applications/Pancho.app
```

```bash
rm -rf ~/Library/Application\ Support/Pancho
```

The second command also deletes the backups Pancho kept of your configuration.

## Troubleshooting

- **I connected Claude and nothing shows up.** Open a new conversation: the ones already open loaded their configuration before you connected.
- **I connected Codex and nothing shows up.** Your approval inside Codex is missing: «Configuración…» → «Ver los pasos de Codex…».
- **«No encuentro Python 3 en este Mac»** (Python 3 not found). Install Apple's tools with `xcode-select --install`.
- **Codex was updated and Pancho no longer sends answers.** Pancho checks that the Codex app is still compatible before sending it anything; if it isn't, it sends nothing and asks you to answer in Codex. Update Pancho (see "Updating" above).
- **I want to check the status from the terminal.**

  ```bash
  python3 ~/pancho/scripts/pancho_install.py status
  ```

  ```bash
  python3 ~/pancho/scripts/pancho_install.py status --codex
  ```

## Known limitations

Pancho is a young personal project. So there are no surprises:

- macOS only.
- The app's interface is in Spanish.
- Codex permissions are still answered in Codex.
- The answer to a Codex question is delivered when Codex finishes its current turn; until then it shows «En cola» (Queued).
- Sending answers to Codex uses the internal channel of the Codex desktop app, which is not a public interface: a Codex update can break it. If that happens, Pancho sends nothing and the question is answered in Codex.
- "Always allow" applies the rule Claude proposes; with very long or compound commands, the rule can be so specific that it never matches again.
- It has been tested with the Claude desktop app (Code tab) and the Codex desktop app.

## For developers

```
app/Pancho/            macOS app (Swift/AppKit)
src/pancho_bridge/     Bridge and installer (Python, standard library)
scripts/               build_pancho.sh, pancho_install.py, pancho_hook.py
design/…/assets/pancho Pancho's animation frames
tests/                 Bridge and installer tests
```

Build without installing:

```bash
zsh scripts/build_pancho.sh
```

Tests (they don't touch your real configuration; they use temporary folders):

```bash
python3 -m unittest discover -s tests -p 'test_pancho*.py'
```

Install or remove the hooks from the terminal, without the window:

```bash
python3 scripts/pancho_install.py install
```

```bash
python3 scripts/pancho_install.py uninstall
```

Add `--codex` for Codex.

## Who is Pancho

Pancho is my dog. I love him so much that I wanted him to help me at work too: now he watches my agents and tells me when they need me.

## License

[MIT](LICENSE). Use it, change it and share it.

Pancho is not affiliated with Anthropic or OpenAI. "Claude" and "Codex" are trademarks of their respective owners.

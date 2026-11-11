# Pancho, the coding dog 🐾

**Una mascota flotante para macOS que te avisa cuando Claude Code o Codex necesitan algo de ti, y te deja contestar sin ir a buscar la ventana.**

<p align="center">
  <img src="media/pancho-demo.gif" alt="Demostración de Pancho" width="420">
</p>

<p align="center">
  Código abierto · macOS · Claude Code + Codex · sin cuentas, sin claves, sin API
</p>

---

## El problema

Trabajas con varios agentes de IA a la vez: tres conversaciones de Claude Code, un par de Codex… Cada uno, tarde o temprano, se para a preguntarte algo o a pedirte permiso. Y tú no te enteras: la ventana está detrás de otra, en otro escritorio o en pantalla completa. El agente se queda media hora esperando una respuesta de dos segundos.

## Qué hace Pancho

Pancho vive en una esquina de tu pantalla, por encima de todo (también de las apps a pantalla completa).

- **Te avisa al momento.** Cuando un agente te hace una pregunta o te pide permiso, Pancho se anima y aparece una tarjeta con el proyecto y la conversación que te necesita.
- **Contestas desde ahí.** Pulsas la tarjeta, eliges una opción o escribes tu respuesta, y le das a Enviar. El agente sigue trabajando en su conversación original, como si hubieras contestado en su propia ventana.
- **Ves todas tus conversaciones de un vistazo.** Una tarjeta por cada conversación en marcha, con su estado: trabajando, esperándote o terminada.
- **Varias a la vez, sin mezclarse.** Cada respuesta vuelve exactamente a la conversación que hizo la pregunta.
- **No pierdes nada.** Si cierras la tarjeta o no contestas, la pregunta vuelve a su app y la respondes allí. Pancho nunca responde por ti ni reenvía nada por su cuenta.

### Qué puedes contestar desde Pancho

| | Claude Code | Codex |
|---|:---:|:---:|
| Preguntas del agente (con opciones o texto libre) | ✅ | ✅ |
| Permisos (Permitir una vez / Permitir siempre / Rechazar) | ✅ | Se contestan en Codex |
| Recuadro «siguiente paso» de la app de Claude | ✅ | — |
| Lista de conversaciones en marcha | ✅ | ✅ |

## Cómo funciona (y por qué no pide claves)

Pancho **no usa ninguna API ni ninguna cuenta**. No habla con servidores de Anthropic ni de OpenAI, y no abre conexiones de red.

Se apoya en los *hooks* oficiales de Claude Code y de Codex: pequeños avisos que esas apps lanzan en tu propio Mac cuando ocurre algo (empieza una conversación, el agente hace una pregunta, pide un permiso, termina un turno). Al pulsar «Conectar», Pancho se apunta a esos avisos. Nada más.

```
Claude Code / Codex  ──(hook local)──▶  puente de Pancho (Python)  ──▶  archivos privados en tu Mac  ──▶  Pancho.app
        ▲                                                                                                   │
        └────────────────────────────── tu respuesta ───────────────────────────────────────────────────────┘
```

- **App**: Swift + AppKit, sin dependencias.
- **Puente**: Python con solo la biblioteca estándar.
- **Datos**: en `~/Library/Application Support/Pancho/`, solo en tu Mac.

## Requisitos

- macOS 13 (Ventura) o posterior.
- Las herramientas de desarrollo de Apple (gratuitas; traen Swift y Python 3). Si no las tienes:

  ```bash
  xcode-select --install
  ```

- Claude Code y/o Codex ya instalados y con tu sesión iniciada, como los uses normalmente.

## Instalación

Hay dos formas. Las dos hacen lo mismo: descargan Pancho en `~/pancho`, lo compilan en tu Mac, lo dejan en `~/Applications/Pancho.app` y lo abren. No piden contraseña y no instalan servicios en segundo plano. Como la app se compila en tu propio equipo, macOS no muestra el aviso de «desarrollador no identificado».

### Opción A: una línea en la Terminal

Abre la app **Terminal**, pega esto y pulsa Intro:

```bash
git clone https://github.com/javierprietoprogram/pancho-the-coding-dog.git ~/pancho && ~/pancho/install.sh
```

Si te dice que faltan las herramientas de Apple, ejecuta `xcode-select --install`, espera a que terminen de instalarse y repite la línea de arriba.

### Opción B: que te lo instale tu agente

Si no quieres abrir la Terminal, pega este texto en una conversación de la **app de escritorio de Claude (pestaña Code)** o de la **app de escritorio de Codex**. Tiene que ser la app de escritorio, en tu Mac: en la web o en una sesión en la nube no funciona, porque Pancho se instala en tu equipo.

```text
Instala Pancho en este Mac. Es una mascota flotante de código abierto que me avisa
cuando Claude Code o Codex necesitan mi respuesta.
Repositorio: https://github.com/javierprietoprogram/pancho-the-coding-dog

Haz esto, en este orden:
1. Comprueba que esto es macOS 13 o posterior y que `xcrun --find swiftc` funciona.
   Si falla, dime que ejecute `xcode-select --install` y para ahí.
2. Si no existe la carpeta ~/pancho, clona el repositorio en ella. Si ya existe,
   entra y ejecuta `git pull`.
3. Lee install.sh y explícame en una frase qué hace antes de ejecutarlo.
4. Ejecuta `~/pancho/install.sh`. Compila la app, la copia a ~/Applications/Pancho.app
   y la abre. Si tu entorno lo bloquea, pídeme permiso para ejecutarlo fuera del sandbox.
5. Comprueba que ~/Applications/Pancho.app existe y que Pancho está abierto.

No toques ~/.claude ni ~/.codex y no ejecutes pancho_install.py: la conexión la hago
yo desde la ventana de Pancho. Al terminar, dime solo esto:
- que pulse «Conectar» en Claude y/o en Codex en la ventana de Pancho;
- que para Codex, además, apruebe los hooks de Pancho en «Review hooks»;
- que abra una conversación nueva para que empiece a funcionar.
```

### Actualizar

Pancho irá recibiendo mejoras. Para tener la última versión:

```bash
cd ~/pancho && git pull && ./install.sh
```

No hace falta volver a conectar Claude ni Codex.

## Conectar Claude y Codex

La primera vez que se abre, Pancho te enseña su ventana de bienvenida («Hola, soy Pancho»). Si la cierras, la tienes siempre en el icono de Pancho de la barra de menús → **Configuración…**

### Claude Code: un clic

1. Pulsa **Conectar** en la fila de Claude.
2. Abre una conversación **nueva** de Claude Code (las que ya tenías abiertas no se enteran hasta que las reinicies).

Ya está. A partir de ahí, las preguntas y los permisos de Claude salen en Pancho.

### Codex: un clic y tu visto bueno

Codex, por seguridad, no deja que ninguna app active hooks sin que tú los apruebes dentro de Codex. Pancho te guía con una ventana paso a paso:

1. Pulsa **Conectar** en la fila de Codex. Se abre la guía.
2. Abre Codex (hay un botón «Abrir Codex» en la propia guía).
3. Pulsa **Review hooks** (aparece junto al cuadro donde escribes; si no lo ves, entra en *Settings > Hooks*).
4. Aprueba las entradas de Pancho: son las que terminan en `pancho_hook.py … --provider codex`.
5. Escribe cualquier cosa en un chat nuevo. En cuanto Codex avise a Pancho, la guía muestra **✓ Listo**.

Solo se hace una vez. Puedes volver a ver los pasos con el botón «Ver los pasos de Codex…».

## Uso diario

- **Mover a Pancho**: arrástralo a donde quieras; las tarjetas se colocan a su alrededor.
- **Contestar**: pulsa la tarjeta de la conversación que te espera, responde y pulsa Enviar.
- **Quitar una conversación de la lista**: con la × de su tarjeta. Vuelve a salir sola si te hace una pregunta nueva.
- **Menú de la barra de menús**: abrir la siguiente pregunta pendiente, actualizar conversaciones, mostrar u ocultar la lista, activar o quitar las animaciones, cambiar el tamaño de Pancho y salir.

### Ajustes (Configuración…)

| Ajuste | Qué hace |
|---|---|
| Abrir Pancho al iniciar sesión | Opcional y desactivado de fábrica. |
| Abrir las preguntas en cuanto lleguen | Si está desactivado, Pancho solo avisa y la pregunta se abre al pulsar su tarjeta. |
| Animaciones de Pancho | Para dejarlo quieto. |
| Tamaño de Pancho | Pequeño, Normal o Grande. |
| Esperar la respuesta | 5, 15 o 30 minutos. Pasado ese tiempo sin contestar en Pancho, la pregunta vuelve a su app. |

## Privacidad y seguridad

- **Sin red, sin cuentas, sin claves.** Pancho no envía nada fuera de tu Mac.
- **No lee tu historial.** No abre las transcripciones de tus conversaciones; solo recibe el aviso concreto de cada pregunta o permiso.
- **Guarda lo mínimo.** El texto de las preguntas que gestiona y tus respuestas, en una carpeta privada de tu usuario.
- **Respeta tu configuración.** Al conectar, solo añade sus propias entradas a `~/.claude/settings.json` y `~/.codex/hooks.json`, y antes guarda una copia de seguridad del archivo. Tus hooks y ajustes no se tocan.
- **Si Pancho está cerrado, no pasa nada.** Los avisos salen en Claude y en Codex como siempre.
- **No pulsa ni escribe en otras apps.** No necesita permisos de Accesibilidad ni de grabación de pantalla.

Un detalle de rendimiento: con Claude conectado, después de cada herramienta que usa el agente se ejecuta un proceso muy corto de Python (así Pancho se entera de que ya diste un permiso en la propia app de Claude).

## Desconectar y desinstalar

**Desconectar**: en Configuración…, pulsa **Desconectar** en Claude o en Codex. Se quitan solo las entradas que añadió Pancho; el resto de tu configuración queda igual.

**Desinstalar del todo** (después de desconectar):

```bash
rm -rf ~/Applications/Pancho.app
```

```bash
rm -rf ~/Library/Application\ Support/Pancho
```

La segunda orden borra también las copias de seguridad que Pancho guardó de tu configuración.

## Solución de problemas

- **He conectado Claude y no sale nada.** Abre una conversación nueva: las que ya estaban abiertas cargaron su configuración antes de conectar.
- **He conectado Codex y no sale nada.** Falta tu visto bueno dentro de Codex: Configuración… → «Ver los pasos de Codex…».
- **«No encuentro Python 3 en este Mac».** Instala las herramientas de Apple con `xcode-select --install`.
- **Codex se ha actualizado y Pancho ya no envía respuestas.** Pancho comprueba que la app de Codex sigue siendo compatible antes de enviarle nada; si no lo es, no envía y te pide contestar en Codex. Actualiza Pancho (mira «Actualizar» más arriba).
- **Quiero comprobar el estado desde la terminal.**

  ```bash
  python3 ~/pancho/scripts/pancho_install.py status
  ```

  ```bash
  python3 ~/pancho/scripts/pancho_install.py status --codex
  ```

## Limitaciones conocidas

Pancho es un proyecto personal y joven. Para que no haya sorpresas:

- Solo macOS.
- Los permisos de Codex todavía se contestan en Codex.
- La respuesta a una pregunta de Codex se entrega cuando Codex termina el turno en curso; mientras tanto aparece «En cola».
- El envío de respuestas a Codex usa el canal interno de la app de escritorio de Codex, que no es una interfaz pública: una actualización de Codex puede romperlo. Si pasa, Pancho no envía nada y la pregunta se contesta en Codex.
- «Permitir siempre» aplica la regla que propone Claude; con órdenes muy largas o compuestas, la regla puede ser tan concreta que no vuelva a servir.
- Está probado con la app de escritorio de Claude (pestaña Code) y con la app de escritorio de Codex.

## Para desarrolladores

```
app/Pancho/            App de macOS (Swift/AppKit)
src/pancho_bridge/     Puente e instalador (Python, biblioteca estándar)
scripts/               build_pancho.sh, pancho_install.py, pancho_hook.py
design/…/assets/pancho Fotogramas de Pancho
tests/                 Pruebas del puente y del instalador
```

Compilar sin instalar:

```bash
zsh scripts/build_pancho.sh
```

Pruebas (no tocan tu configuración real; usan carpetas temporales):

```bash
python3 -m unittest discover -s tests -p 'test_pancho*.py'
```

Instalar o retirar los hooks desde la terminal, sin la ventana:

```bash
python3 scripts/pancho_install.py install
```

```bash
python3 scripts/pancho_install.py uninstall
```

Añade `--codex` para Codex.

## Quién es Pancho

Pancho es mi perro. Le quiero tanto que quise que me ayudara también a trabajar: ahora vigila a mis agentes y me avisa cuando me necesitan.

## Licencia

[MIT](LICENSE). Úsalo, cámbialo y compártelo.

Pancho no está afiliado a Anthropic ni a OpenAI. «Claude» y «Codex» son marcas de sus respectivos dueños.

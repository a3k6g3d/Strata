# Strata desktop (Electron)

Strata's chat page in its own window. It is a thin shell: the page, the sessions and the model all stay in the server.

- Opens `http://127.0.0.1:8080`. If the model is not running it starts it (`launcher`), shows how the loading goes
  (the last lines of the console log), and opens the chat as soon as the server answers.
- A tray icon: Show, Restart the model, Stop the model, Open the console log, Open the settings file, Quit.
  The window's X hides it to the tray (`closeToTray`); Quit ends the app and stops the model it started (`stopOnQuit`).
- One window (a second launch shows the first), window size and place remembered, links to other sites open in the browser.
- The page can ask the window for nothing but copying text (no camera, location or notifications).

## Settings

`%APPDATA%\Strata\desktop.json`, written with the defaults on the first run (tray: Open the settings file):

| key | default | |
| --- | --- | --- |
| `url` | `http://127.0.0.1:8080` | the server |
| `startServer` | `true` | start the model when it is not running |
| `launcher` | `J:\Strata-work\run-strata.ps1` | a PowerShell script that starts the server (it gets `STRATA_NO_OPEN=1`) |
| `stopper` | `J:\Strata-work\Stop-Strata.bat` | stops the model |
| `consoleLog` | `J:\Strata-work\strata-console.txt` | shown while loading |
| `stopOnQuit` | `true` | Quit stops a model this app started |
| `closeToTray` | `true` | the X hides the window |
| `loadTimeoutMinutes` | `10` | after this the loading screen offers Try again / Open the log |

`desktop.log` next to it records what the app tried (opening, starting the launcher, its exit code).

## Build and run

```
npm install
node node_modules/electron/install.js    # fetches the Electron runtime (npm 11 does not run it by itself)
npm start                                # run it
npm run smoke                            # load the page, save a picture and a JSON of what it found, exit
npm run dist                             # dist/Strata-Setup-<version>.exe and dist/Strata-<version>-portable.exe
```

`STRATA_DESKTOP_CONFIG=<file>` uses another settings file (for tests); `STRATA_DESKTOP_DEBUG=1` adds DevTools to the View menu.

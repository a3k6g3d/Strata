# Looking at the interface

The page is plain HTML, CSS and JavaScript with no build step, so it can be looked at without loading a model.
These harnesses are how that is done: a real Chrome driven by Selenium, and a small server that shows one
checkout's `serve/web/` files while every API call goes to the Strata that is already running.

They are not part of the test suite and are not run by anything automatically - they need a browser, a running
server, and a few seconds each. `py -I tools/ui/<file>.py` (selenium is not in the standard library).

| file | what it answers |
| --- | --- |
| `ui-review.py` | runs the page you are editing against the live engine: serves this checkout's `serve/web/` on **8081**, proxies every `/api`, `/mcp`, `/events` call to 8080. Start it, then open `http://127.0.0.1:8081/` - or point Strata's own Browser pane at it, which is how a screenshot of the running app gets into a chat. |
| `ui-look.py` | the big one: walks the whole interface at desktop and phone size, takes the shots, and checks computed styles and focus order. Prints a pass/fail line per check and writes `shots/`. |
| `ui-phone.py` | who is making the page wider than a phone: at 390px it lists every element whose right edge is past the viewport. Sideways overflow is a computed width, not something you can see in the source. Give it more than one URL to compare two builds. |
| `ui-drawer-probe.py` | one control, measured: is the sampling drawer on screen, does it move the page's scroll width, where does focus go, and what does the screen reader get from `aria-hidden`. |
| `ui-panel-check.py` | the side panel's network rule: drives the live app's own Browser pane with four addresses and reports which it showed and which it refused, with the reason it gave. |
| `ui-console-check.py` | does the page come up clean: any JavaScript error on load, and whether every tab still draws its parts (chat boxes, session rows, metric cards, the tab row itself). |
| `ui-guard-check.py` | the "Still writing" notice: asking a second question while another chat's answer writes has to be held and named, not swallowed - and one button has to stop that answer and ask here instead. |
| `ui-stream-check.py` | an answer arriving in the background: it stays in the chat that asked for it, the list marks that chat, opening it shows the live answer rather than the older copy on the server, and a half-typed question stays in the chat it was typed in. |
| `ui-tabs-check.py` | the header tabs: centred at wide widths, and still not overlapping the brand or the controls down to phone width. |
| `ui-composer-check.py` | how tall the message box is at three widths, and whether its controls are still a fingertip on touch. Its height is subtracted from the answer above it, so a slow creep back to a two-line box is invisible until it is complained about. |
| `ui-monitor-check.py` | the request monitor page: a request's card still shows the figures it had, and the expert cache's hit rate is among them. It starts its own mock server with the monitor on, because the live app is usually started without `--api-monitor` and the page answers 404. |
| `ui-webconsole-check.py` | the web console page (`/console`) - the server's output, not the browser's: `ui-console-check.py` reads the browser's own console, this one reads the page.  A line the server prints while the page is open arrives, a line already shown is never shown twice, Pause holds the view and Resume catches up, and nothing hangs off the side at phone width. It starts its own mock server and installs the stdout tee `main()` installs, since that is what fills the buffer. |
| `sprite_usage_check.py` | which glyphs in `serve/web/icons.svg` nothing asks for any more: it reads the sprite, the markup and the script, and lists the ones no page uses. Run it before deleting a glyph, not after - a button can be dropped from the page and its glyph left behind, or the glyph still be wanted by a page you did not look at. |
| `ui-run-check.py` | the clock on a running command: while a tool call is out, the page has to say how long it has been taking and against which limit - and go quiet once the result lands. The answer is a stubbed stream, so it runs in seconds and needs no model. |

`STRATA_UI_URL` says which page to look at (default the review server on 8081, or the live app on 8080 for the
panel check); `STRATA_UI_SHOTS` where the pictures go (default `tools/ui/shots/`, which is not tracked).

Traps these harnesses fell into once, and the reason they are written the way they are:

* `mobile: true` in `Emulation.setDeviceMetricsOverride` makes the viewport a phone width, but it does not make
  `(pointer: coarse)` match on its own - without `Emulation.setTouchEmulationEnabled` the phone row measures the
  desktop branch, so a rule that only exists for a fingertip reads as passing while never being applied.
* `offsetParent` is null for `position: fixed`, so a naive "is it on screen" test calls every overlay invisible.
  They measure the rectangle instead.
* Chrome will not make its window narrower than ~500px, so resizing the window is not a phone. Phone checks use
  `Emulation.setDeviceMetricsOverride`.
* `ui-review.py` is a plain HTTP server: it serves the files as they are on disk, but it reads nothing about the
  *code it runs* after it starts. A fix to the proxy itself only takes effect when the process is restarted - which
  is how a chat that saved fine from the live app kept answering 403 through 8081 hours after the fix landed, and how
  a toast fix that was in the tree kept being reported as broken. Restart it after changing it, and believe the
  browser's error over the source you just read.
* Driving a real local model to call a tool, and catching it mid-flight, costs minutes and misses anyway - the model
  thinks for a minute, the command takes six seconds, and the look lands after it. Checks about the *page's* behaviour
  feed the page a stubbed stream instead (`ui-run-check.py`); leave the model to the checks that need its judgement.


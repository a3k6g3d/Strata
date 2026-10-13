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

`STRATA_UI_URL` says which page to look at (default the review server on 8081, or the live app on 8080 for the
panel check); `STRATA_UI_SHOTS` where the pictures go (default `tools/ui/shots/`, which is not tracked).

Two traps these harnesses fell into once, and the reason they are written the way they are:

* `offsetParent` is null for `position: fixed`, so a naive "is it on screen" test calls every overlay invisible.
  They measure the rectangle instead.
* Chrome will not make its window narrower than ~500px, so resizing the window is not a phone. Phone checks use
  `Emulation.setDeviceMetricsOverride`.

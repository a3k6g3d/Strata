# Learning an expert profile from your own chats

The expert profile (`--expert-profile`) is a ranking of the model's experts. At start the engine fills its GPU cache with the top of the ranking and
pins as many of the next ones in RAM as `--resident-budget-gib` allows; an expert that is in neither has to be read from the model file. On a
machine that keeps only part of the experts in RAM (a 12 GB card with 64 GB of RAM, for example) the file reads are what limit decode speed, so
**which experts are at the top of the ranking matters a lot**.

The shipped profile is built from generic chat. A profile learned from the work you actually do ranks different experts higher. On the machine in
[TUNING_LOG.md](TUNING_LOG.md) it did this, measured on prompts that were not in the learning data:

| profile | agent chats | software work from other projects | general prompts |
|---|---|---|---|
| shipped | 28.4 tok/s, 15.5 file reads per token | 27.1, 17.1 | 30.2, 7.7 |
| learned on your chats **plus general prompts** | 31.6 tok/s, 7.4 reads | 29.5, 8.4 | 29.8, 6.7 |

Two things from those runs to keep in mind: a profile learned from generic prompts alone gave nothing, and one learned from agent chats alone
doubled the file reads on ordinary prompts. Learn from the work you do and put ordinary prompts in the mix.

## What you need

- A server with chats saved (the web app's chats are saved as `s-*.json` files in the sessions folder next to the config), at least a few
  hundred turns' worth: a long agent session is enough, because every tool round counts as a turn.
- The config option `"expert_profile_save": "<file>"` (engine 0.1.36 or newer). The engine then writes what it has learned to that file when it
  quits cleanly. With `"expert_profile_save_every": 0` it writes only at quit, which is what you want here.
- About 30 minutes with the server not otherwise in use.

## Steps

1. Add `"expert_profile_save": "my-profile.bin"` and `"expert_profile_save_every": 0` to the config, and restart the server.
2. Replay your chats against it:

   ```
   python tools/learn_profile_from_chats.py --sessions path/to/sessions --count 120 --held-out held.json
   ```

   This turns the saved chats into replay prompts (the conversation up to each user message or tool result, the newest 3,000 tokens of it),
   replays 120 of them plus 45 general prompts from `bench/profile_corpus.json`, and asks the server to unload the model so the engine writes
   the profile. It also sets aside 24 prompts from the newest quarter of your chats (`held.json`) that it did not learn from.
3. Put the new file in `--expert-profile` (or the config's `args`) and start the server.
4. Measure before and after on the held-out prompts, with the same settings, and **drop the first pass** (the GPU cache is still learning):

   ```
   python tools/learn_profile_from_chats.py --measure held.json --log path/to/the/engine.log
   ```

   Compare the shipped profile and yours. Decode speed on this kind of machine varies by several percent between identical starts, so look at
   `reads_per_token` too: it is much steadier, and a real gain shows up there first (15.5 to 7.4 in the table above).

## Notes

- The mix matters. If your chats are all one kind of work, add the kinds you also do (the `--generic-file` option takes your own list of prompts).
- The profile is a ranking of expert numbers; it contains nothing from your chats. It is specific to one model file, so learn it again after you
  change the model.
- The prompts are replayed against your own server only. Nothing is sent anywhere else.
- The learning run replays at temperature 0.6 with the model's thinking on, like normal use; the measuring run uses temperature 0 so that two
  runs are comparable.
- If you also want the profile to follow a change in your work, learn it again; there is no benefit in learning continuously (a profile written
  at every exit would follow only the last session).

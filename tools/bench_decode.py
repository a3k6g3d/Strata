import json,sys,time,urllib.request
prompts=["Explain how a transformer attention head works, in detail.","Write a short story about a lighthouse keeper who finds a message in a bottle.","List and compare five sorting algorithms with their complexities.","Describe the causes and consequences of the French Revolution.","How does a CPU cache hierarchy work? Give examples.","Write a Python function that parses a CSV file and computes column statistics, with explanation."]
n=int(sys.argv[1]) if len(sys.argv)>1 else 200
tot=0;tt=0
for p in prompts:
    body=json.dumps({"model":"x","messages":[{"role":"user","content":p}],"max_tokens":n,"temperature":0}).encode()
    t=time.time()
    r=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",body,{"Content-Type":"application/json"}),timeout=900))
    dt=time.time()-t; u=r["usage"]; k=u["completion_tokens"]
    print(f"{k:4d} tok {dt:6.1f}s  {k/dt:5.2f} tok/s  prompt {u['prompt_tokens']}",flush=True)
    tot+=k;tt+=dt
print(f"TOTAL {tot} tok {tt:.1f}s = {tot/tt:.2f} tok/s")

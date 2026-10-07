import json,re,sys,time,urllib.request
TRAIN=["Write a Python class implementing an LRU cache with type hints and unit tests.","Explain the Krebs cycle step by step for a biology student.","Draft a polite email declining a job offer while keeping the door open.","Translate into French and explain the grammar: 'If I had known, I would have come earlier.'","Summarize the main arguments of Keynesian versus Austrian economics.","Write a bash script that backs up a directory to a dated tarball and prunes old backups.","Compose a sonnet about autumn rain in a city.","What are the differences between TCP and UDP? Include use cases.","Solve: a train leaves at 3pm at 60 mph, another at 4pm at 80 mph. When does the second catch up? Show work.","Explain how public key cryptography works to a teenager.","Write a SQL query for the top 5 customers by revenue per region, with window functions, and explain it.","Give a day-by-day 5-day itinerary for Kyoto with food recommendations."]
HELD=["Implement binary search in Rust with an explanation of edge cases.","Describe how vaccines train the immune system.","Write a cover letter for a junior data analyst position.","Explain the causes of the 2008 financial crisis.","Write a haiku sequence about the sea, then analyze its imagery.","How do I set up a Docker Compose file for a Flask app with Postgres?","Explain special relativity's time dilation with a worked example.","Write a short dialogue between a detective and a suspect in a noir style."]
sets={"train":TRAIN,"held":HELD}
name=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 200
logp="J:/Strata-work/strata-uncensored.log"
def tiers():
    t=None
    for l in open(logp,errors="ignore"):
        m=re.search(r"expert tiers: GPU (\d+) hits this request; since the start RAM (\d+) blobs, files (\d+) blobs ([\d.]+) MB",l)
        if m: t=tuple(float(x) for x in m.groups())
    return t
prev=tiers() or (0,0,0,0)
tt=0;tk=0;fb=0;mb=0
for p in sets[name]:
    body=json.dumps({"model":"x","messages":[{"role":"user","content":p}],"max_tokens":n,"temperature":0}).encode()
    t=time.time()
    r=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",body,{"Content-Type":"application/json"}),timeout=1200))
    dt=time.time()-t;k=r["usage"]["completion_tokens"]
    time.sleep(1.5); cur=tiers() or prev
    if cur[2]<prev[2]: prev=(0,0,0,0)
    f=cur[2]-prev[2]; m=cur[3]-prev[3]; prev=cur
    print(f"{k:4d} tok {dt:6.1f}s {k/dt:5.2f} tok/s  file blobs/tok {f/k:6.1f}  MB/tok {m/k:6.1f}",flush=True)
    tt+=dt;tk+=k;fb+=f;mb+=m
print(f"[{name}] {tk/tt:.2f} tok/s  file blobs/tok {fb/tk:.1f}  MB/tok {mb/tk:.1f}")

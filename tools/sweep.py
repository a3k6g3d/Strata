import json,os,subprocess,sys,time,urllib.request
base=json.load(open("J:/Strata-work/strata-uncensored.json",encoding="utf-8-sig"))
def kill():
    subprocess.run(["powershell","-c","Get-Process strata -ea 0|Stop-Process -Force; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"|?{$_.CommandLine -match 'serve.server'}|%{Stop-Process -Id $_.ProcessId -Force}"],capture_output=True)
    time.sleep(4)
def setarg(a,k,v):
    a=list(a); i=a.index(k); a[i+1]=v; return a
V={
 "spec6":   ({"--spec":"6"},{}),
 "budget44":({"--resident-budget-gib":"44"},{}),
"budget50":({"--resident-budget-gib":"50"},{}),
 "budget54":({"--resident-budget-gib":"54"},{}),
 "budget28":({"--resident-budget-gib":"28"},{}),
 "fetch16": ({},{"STRATA_FETCH_THREADS":"16"}),
 "fetch32": ({},{"STRATA_FETCH_THREADS":"32"}),
}
for name in sys.argv[1:]:
    ch,env=V[name]; kill()
    c=json.loads(json.dumps(base)); 
    for k,v in ch.items(): c["args"]=setarg(c["args"],k,v)
    c.pop("expert_profile_save",None); c.pop("expert_profile_save_every",None)
    json.dump(c,open(f"J:/Strata-work/cfg-{name}.json","w"),indent=1)
    e=dict(os.environ); e.update(env)
    subprocess.Popen(["python","-m","serve.server","--engine","strata","--config",f"J:/Strata-work/cfg-{name}.json","--port","8080"],cwd="J:/Strata-work/Strata",env=e,stdout=open("J:/Strata-work/server.out","w"),stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(5)
        try: urllib.request.urlopen("http://127.0.0.1:8080/v1/models",timeout=3); break
        except Exception: pass
    print("=====",name,flush=True)
    r=subprocess.run(["python","eval_tiers.py","held","200"],cwd="J:/Strata-work",capture_output=True,text=True)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:],flush=True)
kill()

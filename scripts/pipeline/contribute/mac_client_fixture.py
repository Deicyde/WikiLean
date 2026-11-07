#!/usr/bin/env python3
"""Manual macOS CLI boundary check using only a scripted local provider.

Run with Python 3.12 and the repository's pinned SDK installed. Supply a fresh
--root outside every checkout. The command re-executes inside macOS's network
sandbox, allowing only loopback, and uses an isolated profile plus a fixed
dummy bearer. Permissive user and project settings are planted to check that
the SDK's setting_sources=[] excludes them. It calls no model and does not
use a subscription or API key.

This checks actual CLI permissions and SDK hooks, including hostile tool calls,
without depending on a model choosing to attempt each operation. It does not
measure article judgement quality or subscription usage.
"""
import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,default=Path(__file__).resolve().parents[3])
    parser.add_argument("--root",required=True,type=Path)
    parser.add_argument("--_confined",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if sys.platform!="darwin":
     parser.error("This native platform check requires macOS.")
    if "ANTHROPIC_API_KEY" in os.environ:
     parser.error("Unset ANTHROPIC_API_KEY; this fixture needs no account or key.")
    if not args._confined:
     executable=Path("/usr/bin/sandbox-exec")
     if not executable.is_file():parser.error("macOS sandbox-exec is required for the loopback-only check.")
     env={key:value for key,value in os.environ.items() if key in
          {"PATH","HOME","TMPDIR","LANG","LC_ALL","USER","LOGNAME","TERM"}}
     policy='(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
     command=[str(executable),"-p",policy,sys.executable,str(Path(__file__).resolve()),
              "--source",str(args.source.resolve()),"--root",str(args.root.resolve()),"--_confined"]
     return subprocess.run(command,env=env,check=False).returncode
    source=args.source.resolve(strict=True);root=args.root.resolve()
    if root.is_relative_to(source) or source.is_relative_to(root):
     parser.error("The fresh test directory must be outside the source checkout.")
    root.mkdir(mode=0o700)
    for item in (source/"site",source/"scripts/pipeline/run"):
     sys.path.insert(0,str(item))
    import agent
    import claude_agent_sdk as sdk
    assert Path(agent.__file__).resolve()==source/"scripts/pipeline/run/agent.py"
    run=root/"run";cwd=run/"work/fixture";findings=cwd/"findings/fixture"
    findings.mkdir(parents=True)
    outside=root/"outside";outside.mkdir()
    marker=outside/"outside-only-marker.txt";marker.write_text("fixture-outside-marker")
    link=cwd/"linked-marker.txt";link.symlink_to(marker)
    dir_link=cwd/"linked-outside";dir_link.symlink_to(outside,target_is_directory=True)
    outside_write=outside/"write.json";inside=findings/"result.json"
    write_target=outside/"unchanged-target.txt";write_target.write_text("fixture-outside-marker")
    write_link=findings/"escape-link.json";write_link.symlink_to(write_target)
    shell_marker=outside/"shell-marker.txt"
    calls=[
     ("read-outside","Read",{"file_path":str(marker)}),
     ("read-link","Read",{"file_path":str(link)}),
     ("glob-outside","Glob",{"pattern":"**/*","path":str(outside)}),
     ("grep-outside","Grep",{"pattern":"fixture-outside-marker","path":str(outside),"output_mode":"content"}),
     ("glob-link","Glob",{"pattern":"**/*","path":str(dir_link)}),
     ("grep-link","Grep",{"pattern":"fixture-outside-marker","path":str(dir_link),"output_mode":"content"}),
     ("read-parent-relative","Read",{"file_path":os.path.relpath(marker,cwd)}),
     ("grep-parent-relative","Grep",{"pattern":"fixture-outside-marker","path":os.path.relpath(outside,cwd),"output_mode":"content"}),
     ("glob-absolute-pattern","Glob",{"pattern":str(outside/"*")}),
     ("glob-absolute-pattern-path","Glob",{"pattern":str(outside/"*"),"path":str(cwd)}),
     ("glob-parent-pattern","Glob",{"pattern":os.path.relpath(outside,cwd)+"/*","path":str(cwd)}),
     ("glob-parent-brace","Glob",{"pattern":"{findings,"+os.path.relpath(outside,cwd)+"}/*","path":str(cwd)}),
     ("glob-walk","Glob",{"pattern":"**/outside-only-marker.txt","path":str(cwd)}),
     ("grep-walk","Grep",{"pattern":"fixture-outside-marker","path":str(cwd),"output_mode":"content"}),
     ("write-outside","Write",{"file_path":str(outside_write),"content":'{"fixture":"outside"}'}),
     ("write-relative","Write",{"file_path":"relative-result.json","content":'{"fixture":"relative"}'}),
     ("write-parent-relative","Write",{"file_path":"../parent-result.json","content":'{"fixture":"parent"}'}),
     ("write-link","Write",{"file_path":str(write_link),"content":'{"fixture":"link"}'}),
     ("write-inside","Write",{"file_path":str(inside),"content":'{"fixture":"inside"}'}),
     ("read-inside","Read",{"file_path":str(inside)}),
     ("bash-unlisted","Bash",{"command":"printf fixture > "+shlex.quote(str(shell_marker)),"description":"Synthetic refusal check"}),
     ("edit-unlisted","Edit",{"file_path":str(inside),"old_string":"inside","new_string":"tampered"}),
     ("glob-inside","Glob",{"pattern":"*.json","path":str(findings)}),
     ("grep-inside","Grep",{"pattern":"fixture","path":str(findings),"output_mode":"content"}),
    ]
    DUMMY_AUTH="local-fixture-not-a-credential"
    tool_results={};audit=[];seen_tools=[];state={"main_requests":0,"auxiliary_requests":0,"other_paths":[],"dummy_auth_requests":0,"unexpected_auth":False}
    lock=threading.Lock()
    def raw(value):
     return json.dumps(value,separators=(",",":")).encode()
    def events(model,step,case):
     message={"id":"msg_fixture_"+str(step),"type":"message","role":"assistant","content":[],
              "model":model,"stop_reason":None,"stop_sequence":None,
              "usage":{"input_tokens":1,"output_tokens":1}}
     stream=[("message_start",{"message":message})]
     if case:
      identifier,name,inputs=case
      stream += [("content_block_start",{"index":0,"content_block":{"type":"tool_use","id":identifier,"name":name,"input":{}}}),
                 ("content_block_delta",{"index":0,"delta":{"type":"input_json_delta","partial_json":json.dumps(inputs)}})]
      reason="tool_use"
     else:
      stream += [("content_block_start",{"index":0,"content_block":{"type":"text","text":""}}),
                 ("content_block_delta",{"index":0,"delta":{"type":"text_delta","text":"Fixture complete."}})]
      reason="end_turn"
     stream += [("content_block_stop",{"index":0}),
                ("message_delta",{"delta":{"stop_reason":reason,"stop_sequence":None},"usage":{"output_tokens":1}}),
                ("message_stop",{})]
     return b"".join(("event: "+kind+"\ndata: ").encode()+raw({"type":kind,**data})+b"\n\n" for kind,data in stream)

    class Handler(BaseHTTPRequestHandler):
     def log_message(self,*args):pass
     def send_json(self,status,value):
      data=raw(value);self.send_response(status);self.send_header("Content-Type","application/json")
      self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
     def do_CONNECT(self):
      self.send_json(403,{"error":"fixture prohibits external traffic"})
     def do_GET(self):
      with lock:state["other_paths"].append(self.path.split("?")[0])
      self.send_json(404,{"error":"loopback fixture only"})
     def do_POST(self):
      if self.headers.get("Authorization")!="Bearer "+DUMMY_AUTH or self.headers.get("x-api-key") is not None:
       with lock:state["unexpected_auth"]=True
       self.send_json(403,{"error":"only the dummy fixture bearer is accepted"});return
      with lock:state["dummy_auth_requests"]+=1
      size=int(self.headers.get("Content-Length","0"))
      if size>8*1024*1024:self.send_json(413,{"error":"bounded fixture"});return
      payload=json.loads(self.rfile.read(size) or b"{}")
      if self.path.startswith("/v1/messages/count_tokens"):
       self.send_json(200,{"input_tokens":1});return
      if not self.path.startswith("/v1/messages"):
       with lock:state["other_paths"].append(self.path.split("?")[0])
       self.send_json(404,{"error":"loopback fixture only"});return
      for message in payload.get("messages",[]):
       content=message.get("content",[])
       if not isinstance(content,list):continue
       for block in content:
        if isinstance(block,dict) and block.get("type")=="tool_result":
         identifier=block.get("tool_use_id")
         if identifier in {row[0] for row in calls}:
          tool_results[identifier]={"is_error":bool(block.get("is_error")),"contains_marker":"fixture-outside-marker" in str(block.get("content","")),"contains_outside_filename":"outside-only-marker.txt" in str(block.get("content","")),"contains_inside":"inside" in str(block.get("content","")),"contains_result_filename":"result.json" in str(block.get("content",""))}
      names={tool.get("name") for tool in payload.get("tools",[]) if isinstance(tool,dict)}
      with lock:
       if {"Read","Write"}<=names:
        step=state["main_requests"];state["main_requests"]+=1;case=calls[step] if step<len(calls) else None
       else:
        state["auxiliary_requests"]+=1;step=100+state["auxiliary_requests"];case=None
      body=events(payload.get("model","fixture-model"),step,case)
      self.send_response(200);self.send_header("Content-Type","text/event-stream")
      self.send_header("Content-Length",str(len(body)));self.send_header("Connection","close");self.end_headers()
      self.wfile.write(body);self.wfile.flush();self.close_connection=True

    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    endpoint="http://127.0.0.1:"+str(server.server_address[1])
    # No real credentials: this process has a fresh client profile and a dummy
    # fixture bearer value. ANTHROPIC_API_KEY stays unset.
    assert "ANTHROPIC_API_KEY" not in os.environ
    client_config=root/"credentialless-client";client_config.mkdir(mode=0o700)
    project_config=cwd/".claude";project_config.mkdir(mode=0o700)
    permissive={"permissions":{"allow":["Read(//**)","Write(//**)","Bash(*)"]}}
    for settings in (client_config/"settings.json",project_config/"settings.json"):
        settings.write_text(json.dumps(permissive))
    os.environ["CLAUDE_CONFIG_DIR"]=str(client_config)
    os.environ["ANTHROPIC_BASE_URL"]=endpoint
    os.environ["ANTHROPIC_AUTH_TOKEN"]=DUMMY_AUTH
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]="1"
    os.environ["DISABLE_TELEMETRY"]="1";os.environ["DISABLE_ERROR_REPORTING"]="1"
    os.environ["HTTP_PROXY"]=endpoint;os.environ["HTTPS_PROXY"]=endpoint
    os.environ["NO_PROXY"]="127.0.0.1,localhost"
    original=sdk.query
    async def observe(*,prompt,options):
     assert options.tools==["Read","Grep","Glob","Write"]
     assert options.permission_mode=="dontAsk" and options.setting_sources==[]
     options.max_turns=30
     if "max_budget_usd" in {item.name for item in dataclasses.fields(options)}:options.max_budget_usd=0.25
     matcher,=options.hooks["PreToolUse"];original_hook,=matcher.hooks
     async def hook(data,identifier,context):
      answer=await original_hook(data,identifier,context)
      audit.append({"tool":data.get("tool_name"),"path":(data.get("tool_input") or {}).get("file_path"),
                    "decision":(answer.get("hookSpecificOutput") or {}).get("permissionDecision","defer")})
      return answer
     matcher.hooks=[hook]
     async for message in original(prompt=prompt,options=options):
      if getattr(message,"subtype",None)=="init":seen_tools.extend(message.data.get("tools",[]))
      yield message
    sdk.query=observe
    started=time.monotonic()
    try:
     envelope=agent.call(root=run,task_id="fixture",role="worker",
                         prompt="Perform the controlled compatibility fixture tool calls and finish.",
                         system="Only perform the synthetic compatibility fixture.",
                         model="haiku",execute=True,timeout=90)
    finally:
     server.shutdown();server.server_close();thread.join(timeout=2)
    def denied(identifier):
     item=tool_results.get(identifier,{})
     return bool(item) and item.get("is_error") and not item.get("contains_marker")
    checks={"dummy_auth_only":state["dummy_auth_requests"]>0 and not state["unexpected_auth"],
            "direct_read_outside_refused":denied("read-outside"),
            "symlink_read_outside_refused":denied("read-link"),
            "outside_write_hook_refused":any(item["tool"]=="Write" and item["path"]==str(outside_write) and item["decision"]=="deny" for item in audit),
            "outside_file_absent":not outside_write.exists(),
            "inside_write_succeeded":inside.is_file() and json.loads(inside.read_text())=={"fixture":"inside"},
            "expected_tool_list":set(seen_tools)==set(agent.WORKER_TOOLS),
            "cli_loop_finished":not envelope.get("is_error") and state["main_requests"]>=len(calls)+1}
    for identifier in ("glob-outside","grep-outside","glob-link","grep-link","write-relative","write-parent-relative","write-link",
                       "read-parent-relative","grep-parent-relative","glob-absolute-pattern","glob-absolute-pattern-path",
                       "bash-unlisted","edit-unlisted"):
     checks[identifier+"_refused"]=denied(identifier)
    for identifier,flag in (("glob-parent-pattern","contains_outside_filename"),("glob-parent-brace","contains_outside_filename"),
                            ("glob-walk","contains_outside_filename"),("grep-walk","contains_marker")):
     value=tool_results.get(identifier)
     checks[identifier+"_kept_scope"]=value is not None and (value["is_error"] or not value[flag])
    for identifier,flag in (("read-inside","contains_inside"),("glob-inside","contains_result_filename"),("grep-inside","contains_inside")):
     value=tool_results.get(identifier)
     checks[identifier+"_succeeded"]=value is not None and not value["is_error"] and value[flag]
    checks["relative_files_absent"]=not (cwd/"relative-result.json").exists() and not (cwd.parent/"parent-result.json").exists()
    checks["shell_marker_absent"]=not shell_marker.exists()
    checks["symlink_write_target_unchanged"]=write_target.read_text()=="fixture-outside-marker"
    report={"fixture_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_commit":subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"],text=True).strip(),
            "platform":platform.system(),"macos_version":platform.mac_ver()[0],"architecture":platform.machine(),"sdk_version":importlib.metadata.version("claude-agent-sdk"),
            "provider":"scripted loopback fixture; no model","model_calls":0,
            "uses_isolated_dummy_auth":state["dummy_auth_requests"]>0 and not state["unexpected_auth"],
            "case_count":len(calls),"elapsed_seconds":round(time.monotonic()-started,3),"checks":checks,"requests":state,
            "tool_results":tool_results,"tool_audit":audit,"client_failure":envelope.get("agent_call"),
            "pass":all(checks.values())}
    (root/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({key:value for key,value in report.items()
                      if key not in ("tool_audit","tool_results","client_failure")}))
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())

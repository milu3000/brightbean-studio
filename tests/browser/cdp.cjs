/* Dependency-free CDP pipe transport for an already installed Chromium. */
'use strict';
const {spawn}=require('node:child_process');
class Browser {
    constructor(binary, profile) {
        this.sequence=0;this.pending=new Map();this.handlers=new Map();this.errors=[];this.stderr='';this.closed=false;
        this.process=spawn(binary,[
            '--headless=new','--no-sandbox','--disable-dev-shm-usage','--remote-debugging-pipe',
            '--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update',
            '--disable-sync','--disable-extensions','--disable-default-apps','--metrics-recording-only','--mute-audio',
            '--proxy-server=http://127.0.0.1:9',
            '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+profile,'about:blank'
        ],{stdio:['ignore','ignore','pipe','pipe','pipe']});
        this.process.stderr.setEncoding('utf8').on('data',text=>{this.stderr+=text;});
        let buffer='';
        this.process.stdio[4].setEncoding('utf8').on('data',text=>{
            buffer+=text;let end;
            while((end=buffer.indexOf('\0'))>=0){
                const message=JSON.parse(buffer.slice(0,end));buffer=buffer.slice(end+1);
                if(message.id){const pending=this.pending.get(message.id);if(!pending)continue;
                    clearTimeout(pending.timer);this.pending.delete(message.id);
                    if(message.error)pending.reject(new Error(JSON.stringify(message.error)));else pending.resolve(message.result);
                }else for(const callback of this.handlers.get(message.method)||[])Promise.resolve(callback(message.params,message.sessionId)).catch(error=>this.errors.push(error.stack));
            }
        });
        this.process.stdio[3].on('error',()=>{});
        const stopped=error=>{this.closed=true;for(const pending of this.pending.values()){clearTimeout(pending.timer);pending.reject(error);}this.pending.clear();};
        this.process.once('error',stopped);this.process.once('exit',(code,signal)=>stopped(new Error(`Chrome exited (${code}, ${signal})`)));
    }
    on(name,callback){if(!this.handlers.has(name))this.handlers.set(name,[]);this.handlers.get(name).push(callback);}
    command(method,params={},sessionId){
        if(this.closed)return Promise.reject(new Error('Chrome is closed'));const id=++this.sequence;
        return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('CDP timeout: '+method));},8000);
            this.pending.set(id,{resolve,reject,timer});this.process.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');});
    }
    async close(){if(this.closed)return;const exited=new Promise(resolve=>this.process.once('exit',resolve));this.process.kill('SIGKILL');await exited;}
}
module.exports={Browser};

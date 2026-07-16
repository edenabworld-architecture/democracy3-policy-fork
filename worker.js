export default {
  async fetch(request, env) {
    const url=new URL(request.url);const headers={'content-type':'application/json','access-control-allow-origin':env.ALLOWED_ORIGIN||'*','access-control-allow-headers':'content-type,authorization','access-control-allow-methods':'GET,POST,PATCH,OPTIONS'};
    if(request.method==='OPTIONS')return new Response(null,{headers});
    if(url.pathname==='/submissions'&&request.method==='GET'){
      const policy=url.searchParams.get('policy_id');const result=policy?await env.DB.prepare('SELECT public_id,policy_id,policy_title,type,title,content,source_url,target,status,created_at,updated_at FROM submissions WHERE policy_id=? ORDER BY created_at DESC LIMIT 100').bind(policy).all():await env.DB.prepare('SELECT public_id,policy_id,policy_title,type,title,content,source_url,target,status,created_at,updated_at FROM submissions ORDER BY created_at DESC LIMIT 100').all();return Response.json({entries:result.results},{headers});
    }
    if(url.pathname==='/submissions'&&request.method==='POST'){
      const body=await request.json();for(const key of ['policy_id','policy_title','type','title','content','created_at'])if(!String(body[key]||'').trim())return Response.json({error:`${key} required`},{status:400,headers});
      const allowed=['annotation','correction','evidence','red_team','fork'];if(!allowed.includes(body.type))return Response.json({error:'invalid type'},{status:400,headers});
      const id=crypto.randomUUID();await env.DB.prepare('INSERT INTO submissions(public_id,policy_id,policy_title,type,title,content,source_url,target,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)').bind(id,body.policy_id,body.policy_title,body.type,body.title,body.content,body.source_url||'',body.target||'','접수',body.created_at,new Date().toISOString()).run();return Response.json({submission_id:id,status:'접수'},{status:201,headers});
    }
    return Response.json({error:'not found'},{status:404,headers});
  }
};

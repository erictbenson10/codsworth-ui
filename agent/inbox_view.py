"""Readable source-labelled work overview, without a second work database."""
from fleet_view import overview_text


def inbox_text(snapshot):
    lines=['Corner work · '+str(snapshot.get('observed_at',''))]
    available=snapshot.get('available',{})
    for key,label,command in [('issues','Gitea · example/workspace · issues','/issues'),
                              ('pulls','Gitea · example/workspace · pull requests',None)]:
        lines.append(label)
        if not available.get(key):
            source=snapshot.get('sources',{}).get(key,{}) if isinstance(snapshot.get('sources'),dict) else {}
            tries=source.get('tries')
            lines.append('Unavailable · this is not an empty queue.'
                +('\n'+str(source.get('host','Source'))+' at '+str(source.get('address','unknown address')) if source else '')
                +('\n'+str(tries)+(' try' if tries==1 else ' tries')+', all failed' if isinstance(tries,int) else ''))
            continue
        records=snapshot.get(key,[])
        lines.extend(f"#{item['number']}  {item['title']}\n{item['url']}" for item in records[:8])
        if not records:
            lines.append('No open '+('issues.' if key=='issues' else 'pull requests.'))
        if len(records)>8 or snapshot.get('possibly_more',{}).get(key):
            lines.append('Showing a limited first page. '+('Use /issues to browse.' if command else 'Browse https://git.example.com/example/workspace/pulls'))
    lines.append('Fleet · global activity (all repositories)')
    fleet=snapshot.get('fleet')
    if isinstance(fleet,dict):
        lines.append(overview_text(fleet))
    else:
        lines.append('Fleet unavailable.')
    for error in snapshot.get('errors',[]):
        lines.append(str(error.get('source','Source'))+': '+str(error.get('message','Unavailable')))
    lines.append('/issues NUMBER · /questions · /links then /open N\n/bugs is Codsworth maintenance. /tasks is your local checklist.')
    return '\n\n'.join(lines)

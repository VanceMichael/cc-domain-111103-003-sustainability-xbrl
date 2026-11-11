import xml.etree.ElementTree as ET

def local_name(tag): return tag.rsplit("}",1)[-1]

def inspect_report(raw):
    root=ET.fromstring(raw)
    contexts={}
    units={}
    facts=[]
    for node in root.iter():
        name=local_name(node.tag)
        if name=="context": contexts[node.attrib.get("id")]=node
        elif name=="unit": units[node.attrib.get("id")]=node
        elif node.attrib.get("contextRef"):
            facts.append({"concept":name,"context":node.attrib.get("contextRef"),"unit":node.attrib.get("unitRef"),"value":node.text or ""})
    return {"contexts":contexts,"units":units,"facts":facts}

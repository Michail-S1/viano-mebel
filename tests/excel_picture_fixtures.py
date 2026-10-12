"""Small OOXML fixtures for embedded-image import, not user workbooks."""
import base64
import struct
import zlib
import zipfile
from test_importer import fixture

NSD = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NSA = "http://schemas.openxmlformats.org/drawingml/2006/main"
NSR = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def tiny_png(rgb=(0, 100, 200)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B",1,1,8,2,0,0,0)) + chunk(
        b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b"")


PICTURE = tiny_png()


def picture_price(path, pictures=True, data=PICTURE, no_color=False):
    rows = [{} for _ in range(11)]
    rows[0] = {"C":"МЕБЕЛЬ ОПТ"}
    rows[8] = {"C":46264}
    rows.append({"C":"ФОТО", "D":"НАИМЕНО-\nВАНИЕ", "E":"РАЗМЕР (М)", "F":"ЦВЕТ",
                 "G":"ЦЕНА", "H":"КОЛ-ВО УПАКОВОК", "I":"ВЕС (КГ)"})
    for index, (size, price) in enumerate(zip(
            ("0,4x2,2x0,520","0,5x2,2x0,520","0,8x2,2x0,520","1,0x2,2x0,520",
             "1,2x2,2x0,520","1,5x2,2x0,520 (№1)","1,6x2,2x0,520"),
            (6110,6870,8380,9410,10560,11970,11970))):
        row = {"E":size,"G":price}
        if index == 0:
            row["D"]="Шкаф АВИС (КОРПУС) ЛДСП"
            if not no_color:
                row["F"]="белый древесный\nдуб крафт золотой"
        rows.append(row)
    rows.append({"D":"Комод Лира","E":"0,6x0,4x0,9","F":"Белый","G":6500})
    fixture(path, {"Шкафы":{"rows":rows,"merges":["C13:C19","D13:D19","F13:F19"]}})
    if not pictures:
        return
    # Supplier logo before the header, a NEW badge in D, and the actual photo in C.
    anchors = [(1,7,2,7,False), (12,13,3,3,False), (16,18,2,2,False)]
    add_pictures(path, anchors, data)


def add_pictures(path, anchors, data=PICTURE, external=False):
    xml = [f'<xdr:wsDr xmlns:xdr="{NSD}" xmlns:a="{NSA}" xmlns:r="{NSR}">']
    for index, (first,last,col,last_col,one_cell) in enumerate(anchors,1):
        tag="oneCellAnchor" if one_cell else "twoCellAnchor"
        xml.append(f'<xdr:{tag}><xdr:from><xdr:col>{col}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{first}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>')
        if one_cell:
            xml.append('<xdr:ext cx="100000" cy="200000"/>')
        else:
            xml.append(f'<xdr:to><xdr:col>{last_col}</xdr:col><xdr:colOff>100</xdr:colOff><xdr:row>{last}</xdr:row><xdr:rowOff>100</xdr:rowOff></xdr:to>')
        xml.append(f'<xdr:pic><xdr:blipFill><a:blip r:{"link" if external else "embed"}="rId{index}"/></xdr:blipFill></xdr:pic><xdr:clientData/></xdr:{tag}>')
    xml.append('</xdr:wsDr>')
    with zipfile.ZipFile(path) as archive:
        entries={name:archive.read(name) for name in archive.namelist()}
    worksheet="xl/worksheets/sheet1.xml"
    entries[worksheet]=entries[worksheet].replace(b"</worksheet>",
        f'<drawing xmlns:r="{NSR}" r:id="drawing1"/></worksheet>'.encode())
    rel_ns="http://schemas.openxmlformats.org/package/2006/relationships"
    entries["xl/worksheets/_rels/sheet1.xml.rels"]=f'<Relationships xmlns="{rel_ns}"><Relationship Id="drawing1" Type="{NSR}/drawing" Target="../drawings/drawing1.xml"/></Relationships>'.encode()
    entries["xl/drawings/drawing1.xml"]="".join(xml).encode()
    relationships=f'<Relationships xmlns="{rel_ns}">' + "".join(
        f'<Relationship Id="rId{i}" Type="{NSR}/image" Target="{"https://example.invalid/photo.png" if external else "../media/image"+str(i)+".png"}" {"TargetMode=" + chr(34) + "External" + chr(34) if external else ""}/>'
        for i in range(1,len(anchors)+1)) + '</Relationships>'
    entries["xl/drawings/_rels/drawing1.xml.rels"]=relationships.encode()
    if not external:
        for i in range(1,len(anchors)+1):
            entries[f"xl/media/image{i}.png"]=data
    with zipfile.ZipFile(path,"w") as archive:
        for name,value in entries.items():
            archive.writestr(name,value)

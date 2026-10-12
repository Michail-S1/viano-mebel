"""Header, merged-size and Excel-picture regression checks."""
import base64
import io
import sqlite3
import struct
import sys
import tempfile
import subprocess
from unittest.mock import patch
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.importer import inspect_file, parse_file, product_key
from backend.images import read_pictures, product_pictures, _xls_stream_pictures
from excel_picture_fixtures import picture_price, add_pictures, PICTURE
import test_server as integration


class PictureExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name) / "Регион 058.xlsx"
        picture_price(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def config_products(self):
        config=inspect_file(str(self.path),"fixture")
        config["factory"]="Регион 058"
        return config, parse_file(str(self.path),config)["products"]

    def test_wrapped_header_and_all_size_variants_including_same_price(self):
        config, products=self.config_products()
        sheet=config["sheets"][0]
        self.assertEqual(config["factory"],"Регион 058")
        self.assertTrue(sheet["selected"])
        self.assertEqual({key:sheet["mapping"][key] for key in ("name","price","dimensions","color")},
                         {"name":"D","price":"G","dimensions":"E","color":"F"})
        self.assertEqual(sheet["count"],8)
        self.assertEqual(len(products),8)
        self.assertEqual(len({product_key(item) for item in products}),8)
        self.assertEqual([item["source_row"] for item in products],list(range(13,21)))
        self.assertEqual(products[5]["base_price"],products[6]["base_price"])
        self.assertNotEqual(products[5]["dimensions"],products[6]["dimensions"])
        self.assertTrue(all(item["name"]=="Шкаф АВИС (КОРПУС) ЛДСП" for item in products[:7]))
        self.assertEqual(products[0]["color"],"белый древесный дуб крафт золотой")

    def test_merged_sizes_without_colour_are_kept(self):
        picture_price(self.path,no_color=True)
        _,products=self.config_products()
        self.assertEqual(len(products),8)

    def test_group_photo_shared_by_variants_but_not_logo_badge_or_next_product(self):
        config,products=self.config_products()
        pictures,warnings=read_pictures(self.path)
        self.assertEqual(len(pictures),3)
        matches,warnings=product_pictures(self.path,config,products)
        self.assertEqual(warnings,[])
        self.assertEqual(set(matches),{("Шкафы",row) for row in range(13,20)})
        self.assertTrue(all(len(images)==1 and images[0]["data"]==PICTURE for images in matches.values()))

    def test_one_cell_anchor_and_absent_photos(self):
        picture_price(self.path,pictures=False)
        config,products=self.config_products()
        self.assertEqual(product_pictures(self.path,config,products),({},[]))
        add_pictures(self.path,[(16,16,2,2,True)])
        matches,warnings=product_pictures(self.path,config,products)
        self.assertEqual(len(matches),7)
        self.assertEqual(warnings,[])

    def test_external_photo_is_never_fetched(self):
        picture_price(self.path,pictures=False)
        add_pictures(self.path,[(16,18,2,2,False)],external=True)
        config,products=self.config_products()
        matches,warnings=product_pictures(self.path,config,products)
        self.assertEqual(matches,{})
        self.assertTrue(any("внешнее" in warning for warning in warnings))

    def test_unrelated_workbook_relationship_does_not_disable_photos(self):
        with zipfile.ZipFile(self.path) as archive:
            entries={name:archive.read(name) for name in archive.namelist()}
        key="xl/_rels/workbook.xml.rels"
        entries[key]=entries[key].replace(b"</Relationships>",
            b'<Relationship Id="metadata" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml" Target="../customXml/item1.xml"/></Relationships>')
        with zipfile.ZipFile(self.path,"w") as archive:
            for name,data in entries.items(): archive.writestr(name,data)
        pictures,warnings=read_pictures(self.path)
        self.assertEqual(len(pictures),3)
        self.assertEqual(warnings,[])

    def test_biff_xls_picture_with_split_drawing_group(self):
        def art(kind,payload=b"",instance=0,container=False):
            return struct.pack("<HHI",instance*16+(15 if container else 0),kind,len(payload))+payload
        def record(kind,data):
            return struct.pack("<HH",kind,len(data))+data
        blip=art(0xF01E,b"0"*16+b"\xff"+PICTURE,0x6E0)
        bse=bytearray(36)
        group=art(0xF000,art(0xF001,art(0xF007,bytes(bse)+blip),container=True),container=True)
        properties=art(0xF00B,struct.pack("<HI",0x4104,1),instance=1)
        anchor=art(0xF010,struct.pack("<9H",0,2,0,16,0,2,10,18,10))
        drawing=art(0xF002,art(0xF004,properties+anchor,container=True),container=True)
        name="Шкафы".encode("utf-16-le")
        bounds=struct.pack("<IBBBB",0,0,0,5,1)+name
        prefix=record(0x0085,bounds)+record(0x00EB,group[:40])+record(0x003C,group[40:])+record(0x000A,b"")
        prefix=record(0x0085,struct.pack("<I",len(prefix))+bounds[4:])+prefix[len(record(0x0085,bounds)):]
        stream=prefix+record(0x0809,b"")+record(0x00EC,drawing)+record(0x000A,b"")
        pictures,warnings=_xls_stream_pictures(stream)
        self.assertEqual(warnings,[])
        self.assertEqual(len(pictures),1)
        self.assertEqual(pictures[0].sheet,"Шкафы")
        self.assertEqual(pictures[0].data,PICTURE)
        self.assertEqual((pictures[0].row,pictures[0].last_row,pictures[0].col),(17,19,3))
        # The Росток generator keeps a larger original outer container size.
        stale=bytearray(drawing)
        struct.pack_into("<I",stale,4,len(drawing)+500)
        legacy=prefix+record(0x0809,b"")+record(0x00EC,bytes(stale))+record(0x000A,b"")
        pictures,warnings=_xls_stream_pictures(legacy)
        self.assertEqual(len(pictures),1)
        self.assertEqual(pictures[0].data,PICTURE)


    def test_photo_write_failure_rolls_back_products_and_can_be_retried(self):
        from server import Catalog
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            path=root/"Регион 058.xlsx"
            picture_price(path)
            catalog=Catalog(root/"data")
            catalog.stop_event.set()
            catalog.watcher.join(timeout=2)
            inspected=catalog.inspect({"paths":[str(path)]})
            preview=catalog.preview({"session_id":inspected["session_id"],"files":inspected["files"]})
            original_write=Path.write_bytes
            writes=0
            def failing_write(target,data):
                nonlocal writes
                if target.parent == catalog.photos_dir:
                    writes+=1
                    if writes==2: raise OSError("simulated disk write failure")
                return original_write(target,data)
            with patch.object(Path,"write_bytes",failing_write):
                with self.assertRaisesRegex(OSError,"disk write"):
                    catalog.commit(inspected["session_id"])
            self.assertEqual(catalog.state()["stats"]["total"],0)
            self.assertEqual(list(catalog.photos_dir.iterdir()),[])
            self.assertNotIn("committed",catalog.get_session(inspected["session_id"]))
            result=catalog.commit(inspected["session_id"])
            self.assertEqual(result["added"],8)
            self.assertEqual(result["photos_added"],7)


class ExcelPhotoAPI(unittest.TestCase):
    # Reuse only the fixture lifecycle/helpers, without inheriting unrelated tests.
    setUp=integration.CatalogAPI.setUp
    tearDown=integration.CatalogAPI.tearDown
    start_server=integration.CatalogAPI.start_server
    stop_server=integration.CatalogAPI.stop_server
    request=integration.CatalogAPI.request
    photo=integration.CatalogAPI.photo

    def inspect_preview(self,path,mode="add"):
        inspected=self.request("POST","/api/import/inspect",{"paths":[str(path)]})
        inspected["files"][0]["factory"]="Регион 058"
        preview=self.request("POST","/api/import/preview",{
            "session_id":inspected["session_id"],"files":inspected["files"],"mode":mode})
        return inspected["session_id"],preview

    def products(self):
        return self.request("GET","/api/products?page_size=120")["items"]

    def test_import_preview_repeat_restart_and_backup_restore_preserve_photos(self):
        path=self.root / "Регион 058.xlsx"
        picture_price(path)
        session,preview=self.inspect_preview(path)
        self.assertEqual(preview["counts"]["new"],8)
        self.assertEqual(preview["photos"],{"new":7,"products":7})
        thumbnails=[item["excel_photo_url"] for item in preview["items"] if item.get("excel_photo_url")]
        self.assertEqual(len(thumbnails),7)
        self.assertTrue(self.request("GET",thumbnails[0],raw=True))
        self.assertEqual(self.request("GET","/api/state")["stats"]["total"],0)
        result=self.request("POST","/api/import/commit",{"session_id":session})
        self.assertEqual(result["photos_added"],7)
        first={item["id"]:[photo["id"] for photo in item["photos"]] for item in self.products()}
        self.assertEqual(sum(len(photos) for photos in first.values()),7)
        self.assertEqual(self.request("POST","/api/import/commit",{"session_id":session}),result)
        session,preview=self.inspect_preview(path)
        self.assertEqual(preview["counts"]["duplicates"],8)
        self.assertEqual(preview["photos"]["new"],0)
        self.assertEqual(self.request("POST","/api/import/commit",{"session_id":session})["photos_added"],0)
        self.assertEqual({item["id"]:[photo["id"] for photo in item["photos"]] for item in self.products()},first)
        backup=self.request("GET","/api/backup",raw=True)
        self.request("POST","/api/restore",{"content":base64.b64encode(backup).decode()})
        self.stop_server()
        self.start_server()
        session,preview=self.inspect_preview(path)
        self.assertEqual(preview["photos"]["new"],0)
        self.assertEqual(self.request("POST","/api/import/commit",{"session_id":session})["photos_added"],0)
        self.assertEqual({item["id"]:[photo["id"] for photo in item["photos"]] for item in self.products()},first)

    def test_add_photos_to_existing_duplicates_preserve_manual_primary_and_price(self):
        path=self.root / "Регион 058.xlsx"
        picture_price(path,pictures=False)
        session,preview=self.inspect_preview(path)
        self.request("POST","/api/import/commit",{"session_id":session})
        cabinet=next(item for item in self.products() if item["source_row"]==13)
        manual=self.photo(cabinet["id"])["photos"][0]
        picture_price(path)
        session,preview=self.inspect_preview(path)
        self.assertEqual(preview["counts"]["duplicates"],8)
        self.assertEqual(preview["photos"]["new"],7)
        result=self.request("POST","/api/import/commit",{"session_id":session})
        self.assertEqual(result["photos_added"],7)
        current=self.request("GET","/api/products/"+cabinet["id"])
        self.assertEqual(len(current["photos"]),2)
        self.assertEqual(current["photos"][0]["id"],manual["id"])
        self.assertTrue(current["photos"][0]["is_primary"])
        self.assertEqual(current["base_price"],6110)
        # A later price without photos must keep both photos and the manual primary.
        picture_price(path,pictures=False)
        session,preview=self.inspect_preview(path,"update")
        self.request("POST","/api/import/commit",{"session_id":session})
        current=self.request("GET","/api/products/"+cabinet["id"])
        self.assertEqual(len(current["photos"]),2)
        self.assertEqual(current["photos"][0]["id"],manual["id"])

    def test_invalid_picture_does_not_block_products(self):
        path=self.root / "Регион 058.xlsx"
        picture_price(path,data=b"not an image")
        session,preview=self.inspect_preview(path)
        self.assertEqual(preview["counts"]["new"],8)
        self.assertEqual(preview["photos"]["new"],0)
        self.assertTrue(any("Фото пропущено" in warning for warning in preview["warnings"]))
        result=self.request("POST","/api/import/commit",{"session_id":session})
        self.assertEqual(result["added"],8)
        self.assertEqual(result["photos_added"],0)


if __name__=="__main__":
    unittest.main()

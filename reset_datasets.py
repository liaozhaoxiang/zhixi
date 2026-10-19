"""重置上传数据集：删掉所有 up_ 表和上传文件，切回内置示例数据。"""
import json

from app import datasets

removed = [d["id"] for d in datasets.list_datasets()]
for d in removed:
    datasets.drop(d)
datasets.set_active(None)
print(json.dumps({"removed": removed, "left": len(datasets.list_datasets())}, ensure_ascii=False))

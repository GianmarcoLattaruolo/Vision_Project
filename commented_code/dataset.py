import os
import sys
import re
from math import ceil
from typing import Dict, List, Tuple, Union
from io import BytesIO
import random
from pathlib import Path
from multiprocessing import Pool

import pandas as pd
from PIL import Image
import torchvision
import torch
import msgpack


class MsgPackIterableDatasetMultiTargetWithDynLabels(torch.utils.data.IterableDataset):
    """
    Data source: bunch of msgpack files
    Target values are generated on the fly given a mapping (id->[target1, target, ...])
    """

    def __init__(
        self,
        path: str,
        target_mapping: Dict[str, int],
        key_img_id: str = "id",
        key_img_encoded: str = "image",
        transformation=None,
        shuffle=True,
        meta_path=None,
        cache_size=6 * 4096,
        lat_key="LAT",
        lon_key="LON",
    ):

        super(MsgPackIterableDatasetMultiTargetWithDynLabels, self).__init__()
        self.path = path
        self.cache_size = cache_size
        self.transformation = transformation
        self.shuffle = shuffle
        self.seed = random.randint(1, 100)
        self.key_img_id = key_img_id.encode("utf-8")
        self.key_img_encoded = key_img_encoded.encode("utf-8")
        self.target_mapping = target_mapping

        for k, v in self.target_mapping.items():
            if not isinstance(v, list):
                self.target_mapping[k] = [v]
        if len(self.target_mapping) == 0:
            raise ValueError("No samples found.")

        if not isinstance(self.path, (list, set)):
            self.path = [self.path]

        self.meta_path = meta_path
        if meta_path is not None:
            self.meta = pd.read_csv(meta_path, index_col=0)
            self.meta = self.meta.astype({lat_key: "float32", lon_key: "float32"})
            self.lat_key = lat_key
            self.lon_key = lon_key

        self.shards = self.__init_shards(self.path)
        self.length = len(self.target_mapping)

    @staticmethod
    def __init_shards(path: Union[str, Path]) -> list:
        shards = []
        for i, p in enumerate(path):
            shards_re = r"shard_(\d+).msg"
            shards_index = [
                int(re.match(shards_re, x).group(1))
                for x in os.listdir(p)
                if re.match(shards_re, x)
            ]
            shards.extend(
                [
                    {
                        "path_index": i,
                        "path": p,
                        "shard_index": s,
                        "shard_path": os.path.join(p, f"shard_{s}.msg"),
                    }
                    for s in shards_index
                ]
            )
        if len(shards) == 0:
            raise ValueError("No shards found")
        return shards

    def _process_sample(self, x):
        # prepare image and target value

        # decode and initial resize if necessary
        img = Image.open(BytesIO(x[self.key_img_encoded]))
        if img.mode != "RGB":
            img = img.convert("RGB")

        if img.width > 320 and img.height > 320:
            img = torchvision.transforms.Resize(320)(img)

        # apply all user specified image transformations
        if self.transformation is not None:
            img = self.transformation(img)

        if self.meta_path is None:
            return img, x["target"]
        else:
            _id = x[self.key_img_id].decode("utf-8")
            meta = self.meta.loc[_id]
            return img, x["target"], meta[self.lat_key], meta[self.lon_key]

    def __iter__(self):

        shard_indices = list(range(len(self.shards)))

        if self.shuffle:
            random.seed(self.seed)
            random.shuffle(shard_indices)

        worker_info = torch.utils.data.get_worker_info()

        if worker_info is not None:

            def split_list(alist, splits=1):
                length = len(alist)
                return [
                    alist[i * length // splits : (i + 1) * length // splits]
                    for i in range(splits)
                ]

            shard_indices_split = split_list(shard_indices, worker_info.num_workers)[
                worker_info.id
            ]

        else:
            shard_indices_split = shard_indices

        cache = []

        for shard_index in shard_indices_split:
            shard = self.shards[shard_index]

            with open(
                os.path.join(shard["path"], f"shard_{shard['shard_index']}.msg"), "rb"
            ) as f:
                unpacker = msgpack.Unpacker(
                    f, max_buffer_size=1024 * 1024 * 1024, raw=True
                )
                for x in unpacker:
                    if x is None:
                        continue

                    # valid dataset sample?
                    _id = x[self.key_img_id].decode("utf-8")
                    try:
                        # set target value dynamically
                        if len(self.target_mapping[_id]) == 1:
                            x["target"] = self.target_mapping[_id][0]
                        else:
                            x["target"] = self.target_mapping[_id]
                    except KeyError:
                        # reject sample
                        # print(f'reject {_id} {type(_id)}')
                        continue

                    if len(cache) < self.cache_size:
                        cache.append(x)

                    if len(cache) == self.cache_size:

                        if self.shuffle:
                            random.shuffle(cache)
                        while cache:
                            yield self._process_sample(cache.pop())
        if self.shuffle:
            random.shuffle(cache)

        while cache:
            yield self._process_sample(cache.pop())

    def __len__(self):
        return self.length


class FiveCropImageDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        meta_csv: Union[str, Path, None], #così facendo credo specifichi che l'attributo deve essere o una
        # stringa o un Path object o None
        image_dir: Union[str, Path],
        img_id_col: Union[str, int] = "img_id", #qui non solo specifica il tipo ma da anche un default 
        #value in caso non sia specificato
        allowed_extensions: List[str] = ["jpg", "jpeg", "png"]
    ):
        if isinstance(image_dir, str): #se l'attributo image_dir viene passato come stringa allora lo converte a path
            image_dir = Path(image_dir)
        self.image_dir = image_dir
        self.img_id_col = img_id_col
        self.meta_info = None #questo è praticamente ausiliario per dopo visto che non è passato al 
        #costruttore __init__
        if meta_csv is not None: #se dunque abbiamo dato un file csv come dataset
            print(f"Read {meta_csv}")
            self.meta_info = pd.read_csv(meta_csv) #l'attributo meta_info diventa prorpio il pandas dataframe
            self.meta_info.columns = map(str.lower, self.meta_info.columns)
            # rename column names if necessary to use existing data
            if "lat" in self.meta_info.columns:
                self.meta_info.rename(columns={"lat": "latitude"}, inplace=True) #questi due sono chiari
            if "lon" in self.meta_info.columns:
                self.meta_info.rename(columns={"lon": "longitude"}, inplace=True)
            #qui definisce una nuova colonna del dataset "img_path" tramite l'applicazione di una funzione alla 
            # colonna img_id_col. MA COSA DAVVERO QUESTA FUNZIONA LAMBDA
            self.meta_info["img_path"] = self.meta_info[img_id_col].apply(
                lambda img_id: str(self.image_dir / img_id)
    #gli oggetti di tipo Path si sommano easy dentro str, in pratica sta semplicemnte aggiungendo una colonna
    # con i path intere di ogni immagine
            )
        else:
            image_files = []
            for ext in allowed_extensions:
                image_files.extend([str(p) for p in self.image_dir.glob(f"**/*.{ext}")])#sta creando una lista di 
                #immagini ma non so esattamente come...
            self.meta_info = pd.DataFrame(image_files, columns=["img_path"])
            self.meta_info[self.img_id_col] = self.meta_info["img_path"].apply(
                lambda x: Path(x).stem
            )
    #di fatto dal codice si vede che nel datasets.meta_info non ci sono le immagini in se ma solo i loro path

        self.tfm = torchvision.transforms.Compose(
            [
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize(
                    (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
                ),
            ]
        )

#MA ESATTAMENTE DOV'E' CHE TIRA FUORI I 5 CROP DA OGNI IMMAGINE????????
    def __len__(self):
        return len(self.meta_info.index) # il numero di righe del data_frame


    # ok __getitem__ è un magic method in python quindi fa delle cose diverse dai metodi user defined:
    # The method __getitem__(self, key) defines behavior for when an item is accessed, using the notation self[key]
    def __getitem__(self, idx) -> Tuple[torch.Tensor, dict]: #cosa diamine è -> ?? stack-overflow
        #These are function annotations covered in PEP 3107. Specifically, the -> marks the return function annotation.
        #example at the end
        meta = self.meta_info.iloc[idx]
        meta = meta.to_dict()
        meta["img_id"] = meta[self.img_id_col]

        image = Image.open(meta["img_path"]).convert("RGB")
        image = torchvision.transforms.Resize(256)(image)
        crops = torchvision.transforms.FiveCrop(224)(image) 
        # questo è l'unico punto in cui forse sta facendo questi 5 crop tanto agognati
        crops_transformed = []
        for crop in crops:
            crops_transformed.append(self.tfm(crop))
        return torch.stack(crops_transformed, dim=0), meta #ed ecco la tupla di che si vede nel test notebook

''' EXAMPLE OF -> USAGE
rd={'type':float,'units':'Joules',
    'docstring':'Given mass and velocity returns kinetic energy in Joules'}
def f()->rd:
    pass

>>> f.__annotations__['return']['type']
<class 'float'>
>>> f.__annotations__['return']['units']
'Joules'
>>> f.__annotations__['return']['docstring']
'Given mass and velocity returns kinetic energy in Joules'
'''

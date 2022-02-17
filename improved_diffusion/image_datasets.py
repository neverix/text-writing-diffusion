import string, os, random, json
from PIL import Image
import blobfile as bf
from mpi4py import MPI
import numpy as np
from torch.utils.data import DataLoader, Dataset
import torch.nn.functional as F
import torchvision.transforms as T
from .crop import RandomResizedProtectedCropLazy

import tokenizers


def make_char_level_tokenizer():
    tokenizer = tokenizers.Tokenizer(tokenizers.models.BPE(unk_token="<unk>"))
    trainer = tokenizers.trainers.BpeTrainer(special_tokens=["<s>", "</s>", "<unk>", "<pad>", "<mask>"])
    tokenizer.train_from_iterator([[c] for c in string.printable], trainer)
    tokenizer.post_processor = tokenizers.processors.TemplateProcessing(
        "<s> $0 </s>", special_tokens=[("<s>", 0), ("</s>", 1)]
    )
    return tokenizer


def load_tokenizer(tokenizer_path  = "tokenizer_file", max_seq_len=64, char_level=False):
    if char_level:
        tokenizer = make_char_level_tokenizer()
    else:
        tokenizer = tokenizers.Tokenizer.from_file(tokenizer_path)
    tokenizer.enable_truncation(max_seq_len)
    tokenizer.enable_padding()
    return tokenizer


def tokenize(tokenizer, txt):
    return [t.ids for t in tokenizer.encode_batch(txt)]


def load_data(
    *, data_dir, batch_size, image_size, class_cond=False, deterministic=False,
    txt=False, monochrome=False, offset=0, min_filesize=0,
    txt_pdrop=0., txt_drop_string='<mask><mask><mask><mask>',
    crop_prob=0., crop_min_scale=0.75, crop_max_scale=1.,
    use_special_crop_for_empty_string=False,
    crop_prob_es=0., crop_min_scale_es=0.25, crop_max_scale_es=1.,
    safebox_path=""
):
    """
    For a dataset, create a generator over (images, kwargs) pairs.

    Each images is an NCHW float tensor, and the kwargs dict contains zero or
    more keys, each of which map to a batched Tensor of their own.
    The kwargs dict can be used for class labels, in which case the key is "y"
    and the values are integer tensors of class labels.

    :param data_dir: a dataset directory.
    :param batch_size: the batch size of each returned pair.
    :param image_size: the size to which images are resized.
    :param class_cond: if True, include a "y" key in returned dicts for class
                       label. If classes are not available and this is true, an
                       exception will be raised.
    :param deterministic: if True, yield results in a deterministic order.
    """
    if not data_dir:
        raise ValueError("unspecified data directory")

    safeboxes = None
    if safebox_path and os.path.exists(safebox_path):
        print('using safebox_path')
        with open(safebox_path, 'r') as f:
            safeboxes = json.load(safebox_path)

    all_files, image_file_to_text_file, file_sizes, image_file_to_safebox = _list_image_files_recursively(data_dir, txt=txt, min_filesize=min_filesize, safeboxes=safeboxes)
    print(f"found {len(all_files)} images, {len(image_file_to_text_file)} texts")
    all_files = all_files[offset:]

    n_texts = sum(1 for k in file_sizes.keys() if k.endswith('.txt'))  # sanity check
    n_nonempty_texts = sum(file_sizes[k] > 0 for k in file_sizes.keys() if k.endswith('.txt'))
    n_empty_texts = n_texts - n_nonempty_texts

    if n_texts > 0:
        frac_empty = n_empty_texts/n_texts
        frac_nonempty = n_nonempty_texts/n_texts

        print(f"of {n_texts} texts, {n_empty_texts} ({frac_empty:.1%}) are empty, {n_nonempty_texts} ({frac_nonempty:.1%}) are nonempty")
        print(f"of {n_nonempty_texts}, {len(image_file_to_safebox)} have safeboxes")

    classes = None
    if class_cond:
        # Assume classes are the first part of the filename,
        # before an underscore.
        class_names = [bf.basename(path).split("_")[0] for vpath in all_files]
        sorted_classes = {x: i for i, x in enumerate(sorted(set(class_names)))}
        classes = [sorted_classes[x] for x in class_names]

    pre_resize_transform = None
    pre_resize_transform_for_empty_string = None

    if crop_prob > 0:
        print("using crop")
        if safeboxes is not None:
            print('using safebox crop')
            imode, tsize = (T.functional.InterpolationMode.BICUBIC, (image_size,))
            def safebox_crop(img, safebox):
                return T.RandomApply(
                    transforms=[
                        T.RandomResizedProtectedCropLazy(size=tsize, min_area=crop_min_scale, max_area=crop_max_scale, interpolation=imode),
                    ],
                    p=crop_prob
                )(img, safebox)
            pre_resize_transform = safebox_crop
            if (not use_special_crop_for_empty_string) or (crop_prob_es <= 0):
                use_special_crop_for_empty_string = True
                crop_prob_es = crop_prob
                crop_min_scale_es = crop_min_scale
                crop_max_scale_es = crop_max_scale
        else:
            imode, tsize = (T.functional.InterpolationMode.BICUBIC, (image_size,))
            pre_resize_transform = T.RandomApply(
                transforms=[
                    T.RandomResizedCrop(size=tsize, ratio=(1, 1), scale=(crop_min_scale, crop_max_scale), interpolation=imode),
                ],
                p=crop_prob
            )

    if use_special_crop_for_empty_string and crop_prob_es > 0:
        print("using es crop")
        imode, tsize = (T.functional.InterpolationMode.BICUBIC, (image_size,))
        pre_resize_transform_for_empty_string = T.RandomApply(
            transforms=[
                T.RandomResizedCrop(size=tsize, ratio=(1, 1), scale=(crop_min_scale_es, crop_max_scale_es), interpolation=imode),
            ],
            p=crop_prob_es
        )

    dataset = ImageDataset(
        image_size,
        all_files,
        classes=classes,
        image_file_to_text_file=image_file_to_text_file,
        txt=txt,
        monochrome=monochrome,
        file_sizes=file_sizes,
        shard=MPI.COMM_WORLD.Get_rank(),
        num_shards=MPI.COMM_WORLD.Get_size(),
        txt_pdrop=txt_pdrop,
        txt_drop_string=txt_drop_string,
        pre_resize_transform=pre_resize_transform,
        pre_resize_transform_for_empty_string=pre_resize_transform_for_empty_string,
        image_file_to_safebox=image_file_to_safebox,
    )
    if deterministic:
        loader = DataLoader(
            dataset, batch_size=batch_size, shuffle=False, num_workers=1, drop_last=True,
        )
    else:
        loader = DataLoader(
            dataset, batch_size=batch_size, shuffle=True, num_workers=1, drop_last=True,
        )
    while True:
        yield from loader


def load_superres_data(data_dir, batch_size, large_size, small_size, class_cond=False, txt=False, monochrome=False,
                       deterministic=False, offset=0, colorize=False,
                       blur_prob=0., blur_sigma_min=0.4, blur_sigma_max=0.6,
                       min_filesize=0,
                       txt_pdrop=0., txt_drop_string='<mask><mask><mask><mask>'
                       ):
    data = load_data(
        data_dir=data_dir,
        batch_size=batch_size,
        image_size=large_size,
        class_cond=class_cond,
        txt=txt,
        monochrome=monochrome,
        deterministic=deterministic,
        offset=offset,
        min_filesize=min_filesize,
        txt_pdrop=txt_pdrop,
        txt_drop_string=txt_drop_string,
    )
    for large_batch, model_kwargs in data:
        blurrer = T.RandomApply(transforms=[T.GaussianBlur(5, sigma=(blur_sigma_min, blur_sigma_max))], p=blur_prob)

        model_kwargs["low_res"] = F.interpolate(large_batch, small_size, mode="area")
        if colorize:
            model_kwargs["low_res"] = model_kwargs["low_res"].mean(dim=1, keepdim=True)
        if blur_prob > 0:
            model_kwargs["low_res"] = blurrer(model_kwargs["low_res"])

        yield large_batch, model_kwargs


def _list_image_files_recursively(data_dir, txt=False, min_filesize=0, safeboxes=None):
    results = []
    image_file_to_text_file = {}
    file_sizes = {}
    image_file_to_safebox = {}
    if safeboxes is None:
        safeboxes = {}
    for entry in sorted(bf.listdir(data_dir)):
        full_path = bf.join(data_dir, entry)
        prefix, _, ext = entry.rpartition(".")
        safebox_key = prefix.replace('/', '_')
        if "." in entry and ext.lower() in ["jpg", "jpeg", "png", "gif"]:
            if min_filesize > 0:
                filesize = os.path.getsize(full_path)
                if filesize < min_filesize:
                    continue
                file_sizes[full_path] = filesize
            results.append(full_path)
            if txt:
                prefix, _, ext = full_path.rpartition(".")
                path_txt = prefix + ".txt"
                # print(f'made path_txt={repr(path_txt)} from {repr(entry)}')
                if bf.exists(path_txt):
                    image_file_to_text_file[full_path] = path_txt
                    filesize = os.path.getsize(path_txt)
                    file_sizes[path_txt] = filesize

                    image_file_to_safebox[full_path] = safeboxes.get(safebox_key)
                else:
                    pass
                    # raise ValueError(path_txt)
        elif bf.isdir(full_path):
            next_results, next_map, next_file_sizes, next_image_file_to_safebox = _list_image_files_recursively(full_path, txt=txt)
            results.extend(next_results)
            image_file_to_text_file.update(next_map)
            file_sizes.update(next_file_sizes)
            image_file_to_safebox.update(next_image_file_to_safebox)
    image_file_to_safebox = {k: v for k, v in image_file_to_safebox.items() if v is not None}
    return results, image_file_to_text_file, file_sizes, image_file_to_safebox


class ImageDataset(Dataset):
    def __init__(self, resolution, image_paths,
                 classes=None,
                 image_file_to_text_file=None,
                 txt=False,
                 monochrome=False,
                 file_sizes=None,
                 shard=0, num_shards=1,
                 txt_pdrop=0.,
                 txt_drop_string='<mask><mask><mask><mask>',
                 empty_string_to_drop_string=False,  # unconditional != no text
                 pre_resize_transform=None,
                 pre_resize_transform_for_empty_string=None,
                 image_file_to_safebox=None,
                 ):
        super().__init__()
        self.resolution = resolution
        self.local_images = image_paths[shard:][::num_shards]
        self.local_classes = None if classes is None else classes[shard:][::num_shards]
        self.txt = txt
        self.monochrome = monochrome
        self.file_sizes = file_sizes
        self.txt_pdrop = txt_pdrop
        self.txt_drop_string = txt_drop_string
        self.empty_string_to_drop_string = empty_string_to_drop_string
        self.pre_resize_transform = pre_resize_transform
        if pre_resize_transform_for_empty_string is None:
            pre_resize_transform_for_empty_string = pre_resize_transform
        self.pre_resize_transform_for_empty_string = pre_resize_transform_for_empty_string
        self.image_file_to_safebox = image_file_to_safebox

        if self.txt:
            self.local_images = [p for p in self.local_images if p in image_file_to_text_file]
            self.local_texts = [image_file_to_text_file[p] for p in self.local_images]

    def __len__(self):
        return len(self.local_images)

    def __getitem__(self, idx):
        path = self.local_images[idx]
        with bf.BlobFile(path, "rb") as f:
            pil_image = Image.open(f)
            pil_image.load()

        text = None
        if self.txt:
            path_txt = self.local_texts[idx]
            with bf.BlobFile(path_txt, "r") as f:
                text = f.read()

        if self.pre_resize_transform is not None:
            if self.txt and len(text) == 0:
                pil_image = self.pre_resize_transform_for_empty_string(pil_image)
            else:
                if image_file_to_safebox is not None:
                    if path in self.image_file_to_safebox:
                        print('hit')
                        safebox = self.image_file_to_safebox[path]
                        self.pre_resize_transform(pil_image, safebox)
                    else:
                        print('miss')
                else:
                    pil_image = self.pre_resize_transform(pil_image)

        # We are not on a new enough PIL to support the `reducing_gap`
        # argument, which uses BOX downsampling at powers of two first.
        # Thus, we do it by hand to improve downsample quality.
        while min(*pil_image.size) >= 2 * self.resolution:
            pil_image = pil_image.resize(
                tuple(x // 2 for x in pil_image.size), resample=Image.BOX
            )

        scale = self.resolution / min(*pil_image.size)
        pil_image = pil_image.resize(
            tuple(round(x * scale) for x in pil_image.size), resample=Image.BICUBIC
        )

        mode = "L" if self.monochrome else "RGB"
        arr = np.array(pil_image.convert(mode))
        if self.monochrome:
            arr = np.expand_dims(arr, 2)
        crop_y = (arr.shape[0] - self.resolution) // 2
        crop_x = (arr.shape[1] - self.resolution) // 2
        arr = arr[crop_y : crop_y + self.resolution, crop_x : crop_x + self.resolution]
        arr = arr.astype(np.float32) / 127.5 - 1

        out_dict = {}
        if self.local_classes is not None:
            out_dict["y"] = np.array(self.local_classes[idx], dtype=np.int64)
        if self.txt:
            if (self.txt_pdrop > 0) and (random.random() < self.txt_pdrop):
                text = self.txt_drop_string
            if (len(text) == 0) and self.empty_string_to_drop_string:
                text = self.txt_drop_string
            out_dict['txt'] = text
        return np.transpose(arr, [2, 0, 1]), out_dict

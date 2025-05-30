import argparse
import os
from PIL import Image
import shutil
from torch.utils.data import DataLoader
from torch import optim, autograd
from torchvision import transforms, utils
import torchvision as tv
import torch.nn as nn
import torch
from torch.nn import functional as F
from collections import OrderedDict
import sys
import warnings
import torch.backends.cudnn as cudnn
import random
from glob import glob
import torch.utils.data as data
import numpy as np
import cv2
from tqdm import tqdm

from vqvae import VQVAE
from ulib.utils import read_single_image
import distributed as dist

from submit import _create_run_dir_local, _copy_dir
from logger import Logger
# from ebm import EBM
from model.stylegan1 import EBM, EBM_CAttn

import kornia.color as color
import albumentations as A
# Added for fundus datasets
from dataloader.ms_fundus.fundus_dataloader import FundusSegmentation
from dataloader.ms_fundus import fundus_transforms as tr
from dataloader.ms_prostate.convert_csv_to_list import convert_labeled_list
from dataloader.ms_prostate.PROSTATE_dataloader import PROSTATE_dataset

IMG_EXTENSIONS = [
	'.jpg', '.JPG', '.jpeg', '.JPEG',
	'.png', '.PNG', '.ppm', '.PPM', '.bmp', '.BMP',
	'.tif', '.TIF', '.tiff', '.TIFF',
]
policy = 'color,translation,cutout'

def ema(model1, model2, decay=0.999):
	par1 = dict(model1.named_parameters())
	par2 = dict(model2.named_parameters())

	for k in par1.keys():
		par1[k].data.mul_(decay).add_(par2[k].data, alpha=1 - decay)


def is_image_file(filename):
	return any(filename.endswith(extension) for extension in IMG_EXTENSIONS)


def make_dataset(dir, max_dataset_size=float("inf")):
	images = []
	assert os.path.isdir(dir), '%s is not a valid directory' % dir

	for root, _, fnames in sorted(os.walk(dir)):
		for fname in fnames:
			if is_image_file(fname):
				path = os.path.join(root, fname)
				images.append(path)
	return images[:min(max_dataset_size, len(images))]


def default_loader(path):
	return Image.open(path).convert('RGB')


class ImageFolder(data.Dataset):

	def __init__(self, root, transform=None, return_paths=False,
				 loader=default_loader):
		imgs = make_dataset(root)
		if len(imgs) == 0:
			raise (RuntimeError("Found 0 images in: " + root + "\n"
															   "Supported image extensions are: " + ",".join(
				IMG_EXTENSIONS)))

		self.root = root
		self.imgs = imgs
		self.transform = transform
		self.return_paths = return_paths
		self.loader = loader

	def __getitem__(self, index):
		path = self.imgs[index]
		img = self.loader(path)
		if self.transform is not None:
			img = self.transform(img)
		if self.return_paths:
			return img, path
		else:
			return img

	def __len__(self):
		return len(self.imgs)


def requires_grad(model, flag=True):
	for p in model.parameters():
		p.requires_grad = flag


def load_model(args, checkpoint, device):
	ckpt = torch.load(checkpoint)

	model = VQVAE(embed_dim=args.embed_dim, n_embed=args.n_embed, noise=args.noise)

	new_state_dict = OrderedDict()
	for k, v in ckpt.items():
		name = k.replace("module.", "")  # remove 'module.' of dataparallel
		new_state_dict[name] = v

	model.load_state_dict(new_state_dict)
	model = model.to(device)

	return model

def g_nonsaturating_loss(fake_pred):
	loss = F.softplus(-fake_pred).mean()
	return loss

def d_logistic_loss(real_pred, fake_pred):
	real_loss = F.softplus(-real_pred)
	fake_loss = F.softplus(fake_pred)
	return real_loss.mean() + fake_loss.mean()


def d_r1_loss(real_pred, real_img):
	grad_real, = autograd.grad(
		outputs=real_pred.sum(), inputs=real_img, create_graph=True
	)
	grad_penalty = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()

	return grad_penalty


def langvin_sampler(model, x, y, langevin_steps=20, lr=1.0, sigma=0e-2, step=4, gamma=0.):
	x = x.clone().detach()
	x.requires_grad_(True)
	sgd = optim.SGD([x], lr=lr)
	for k in range(langevin_steps):
		model.zero_grad()
		sgd.zero_grad()
		energy = model(x, y).sum()
		(-energy).backward()
		sgd.step()

	return x.clone().detach()

def inference_langvin_sampler(model, x, y, langevin_steps=20, lr=1.0, sigma=0e-2, step=4, gamma=0., num_save=5):
	x = x.clone().detach()
	x.requires_grad_(True)
	sgd = optim.SGD([x], lr=lr)
	intermediate = []
	for k in range(langevin_steps):
		model.zero_grad()
		sgd.zero_grad()
		energy = model(x, y).sum()

		(-energy).backward()
		sgd.step()
		if k % num_save == 0 and k != 0:
			intermediate.append(x.clone().detach())

	return intermediate

def dec_langvin_sampler(model, x, y, langevin_steps=10, lr=0.1, sigma=0e-2, step=4, gamma=0.):
	x = x.clone().detach()
	x.requires_grad_(True)
	sgd = optim.SGD([x], lr=lr)
	mse_loss = nn.MSELoss(reduction='mean')
	for k in range(langevin_steps):
		model.zero_grad()
		sgd.zero_grad()
		loss = mse_loss(x, y)
		loss.backward()
		sgd.step()

	return x.clone().detach()

def check_folder(log_dir):
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	else:
		shutil.rmtree(log_dir)
		os.makedirs(log_dir)
	return log_dir


def test_image_folder(args, ae, ebm, refine_ebm=None, iteration=0, im_size=256, device='cuda'):
	data_root = os.path.join(args.data_root, args.dataset)
	source_files = list(glob(f'{data_root}/test/{args.source}/*.*'))
	root = os.path.join(args.run_dir, '{:06d}'.format(iteration))
	check_folder(root)
	requires_grad(ebm, False)
	for i, file in enumerate(source_files):
		if i == 1000:
			break
		if is_image_file(file):
			img_name = file.split("/")[-1]
			image = read_single_image(file, im_size=im_size, resize=True).to(device)
			latent = ae.latent(image)

			latent_q = langvin_sampler(ebm, latent, langevin_steps=args.langevin_step, lr=args.langevin_lr)
			image_t = ae.dec(latent_q)

			image_pair = torch.cat((image, image_t), dim=0)
			if args.refine:
				image_refined = langvin_sampler(refine_ebm, image_t.clone().detach())
				image_pair = torch.cat((image_pair, image_refined), dim=0)
			tv.utils.save_image(image_pair, os.path.join(root, img_name), padding=0, normalize=True, range=(-1, 1),
								nrow=1)

def normalize_to_unit_range(tensor):
    """
    Normalize tensor from range (-1, 1) to range (0, 1).
    """
    return (tensor + 1) / 2

def normalize_to_neg_one_one(tensor):
    """
    Normalize tensor from range (0, 1) to range (-1, 1).
    """
    return tensor * 2 - 1

def normalize_lab_tensor(tensor):
    """
    Normalize a B, C, H, W tensor with LAB format to range [-1, 1].
    
    Args:
    tensor (torch.Tensor): Input tensor with shape (B, C, H, W)
    
    Returns:
    torch.Tensor: Normalized tensor with the same shape
    """
    # Check if tensor has the right number of channels
    assert tensor.size(1) == 3, "Input tensor must have 3 channels (L, a, b)"
    
    # Normalize L channel from [0, 100] to [-1, 1]
    tensor[:, 0, :, :] = (tensor[:, 0, :, :] / 50.0) - 1.0
    
    # Normalize a and b channels from [-128, 127] to [-1, 1]
    tensor[:, 1, :, :] = (tensor[:, 1, :, :] + 128) / 127.5 - 1.0
    tensor[:, 2, :, :] = (tensor[:, 2, :, :] + 128) / 127.5 - 1.0
    
    return tensor

def denormalize_lab_tensor(tensor):
    """
    Denormalize a B, C, H, W tensor from range [-1, 1] back to original LAB format.
    
    Args:
    tensor (torch.Tensor): Input tensor with shape (B, C, H, W)
    
    Returns:
    torch.Tensor: Denormalized tensor with the same shape
    """
    # Check if tensor has the right number of channels
    assert tensor.size(1) == 3, "Input tensor must have 3 channels (L, a, b)"
    
    # Denormalize L channel from [-1, 1] to [0, 100]
    tensor[:, 0, :, :] = (tensor[:, 0, :, :] + 1.0) * 50.0
    
    # Denormalize a and b channels from [-1, 1] to [-128, 127]
    tensor[:, 1, :, :] = (tensor[:, 1, :, :] + 1.0) * 127.5 - 128
    tensor[:, 2, :, :] = (tensor[:, 2, :, :] + 1.0) * 127.5 - 128
    
    return tensor

def rgb_to_lab(rgb_tensor):
    """
    Convert a RGB torch tensor in BCHW format from range (-1, 1) to LAB format.
    """
    # Normalize from (-1, 1) to (0, 1)
    rgb_tensor_unit = normalize_to_unit_range(rgb_tensor)
    
    # Convert from RGB to LAB
    lab_tensor = color.rgb_to_lab(rgb_tensor_unit)
    
    return lab_tensor

def lab_to_rgb(lab_tensor):
    """
    Convert a LAB torch tensor in BCHW format to RGB format and normalize back to (-1, 1).
    """
    # Convert from LAB to RGB
    rgb_tensor_unit = color.lab_to_rgb(lab_tensor)
    
    # Normalize from (0, 1) to (-1, 1)
    rgb_tensor = normalize_to_neg_one_one(rgb_tensor_unit)
    
    return rgb_tensor

def main(args):
	ckpt = args.ae_ckpt
	device = "cuda"
	batch_size = args.batch_size
	args.distributed = dist.get_world_size() > 1
	
	if args.dataset == 'fundus':
		composed_transforms_tr = transforms.Compose([
			tr.RandomScaleCrop(256),
			tr.Normalize_tf_normal(),
			tr.ToTensor()
		])
	elif args.dataset == 'prostate':
		composed_transforms_tr = A.Compose([											
											A.RandomSizedCrop(min_max_height=(300,330), height=384, width=384, p=0.3),
										])

    # dataloader config
	if args.dataset == 'fundus':
		source_dataset =  FundusSegmentation(base_dir=args.data_path, phase='train', splitid=args.src_split_id, transform=composed_transforms_tr)
		source_loader = DataLoader(source_dataset, batch_size=args.batch_size, num_workers=0, drop_last=False)
	elif args.dataset == 'prostate':
		source_csv = []
		source_csv.append(args.source + '.csv')
		sr_img_list, sr_label_list = convert_labeled_list(args.data_path, source_csv)
		source_dataset = PROSTATE_dataset(args.data_path, sr_img_list, sr_label_list,
									target_size=384, batch_size=args.batch_size, img_normalize=True, transform=composed_transforms_tr)
		source_loader = DataLoader(dataset=source_dataset,
							batch_size=args.batch_size,
							pin_memory=True,
							num_workers=0,
							drop_last=False)
	
	# Define models
	ae = load_model(args, ckpt, device='cuda')
	ae.eval()

	latent_ebm = EBM_CAttn(size=64, channel_multiplier=args.channel_mul, input_channel=args.embed_dim*2,
					 add_attention=args.attention,
					 spectral=args.sn, cam=args.cam, dataset=args.dataset).to(device)

	# load weights from args.ebm_ckpt
	latent_ebm.load_state_dict(torch.load(args.ebm_ckpt))

	refine_ebm = None
	if args.refine:
		refine_ebm = EBM(size=256, channel_multiplier=1, input_channel=3, add_attention=args.attention,
						 spectral=args.sn, cam=args.cam).to(device)
		refine_optimizer = optim.Adam(refine_ebm.parameters(), lr=args.lr, betas=(args.beta1, args.beta2))

	used_sample = 0
	iterations = -1
	nrow = min(batch_size, 4)

	save_dir = os.path.join(args.data_root, args.dataset, args.expt_name, 'Domain'+ args.source + '2' + args.target)
	os.makedirs(save_dir, exist_ok=True)
	os.makedirs(os.path.join(save_dir, 'image'), exist_ok=True)
	os.makedirs(os.path.join(save_dir, 'mask'), exist_ok=True)
	
	for i, batch in enumerate(tqdm(source_loader)):

		source = batch

		source_img, source_label = source['image'].to(device), source['label'].to(device)

		if args.dataset == 'prostate':
			source_label = source_label.repeat(1, 3, 1, 1)
		LAB_source_img = rgb_to_lab(source_img)

		if args.color_space == 'LAB':
			LAB_source_img = normalize_lab_tensor(LAB_source_img)
			source_img = LAB_source_img
		
		L_channel_source_img = LAB_source_img[:, 0:1, :, :]

		source_latent, source_label_latent = ae.latent(source_img), ae.latent(source_label)

		requires_grad(latent_ebm, False)
		source_latent_q = inference_langvin_sampler(latent_ebm, source_latent.clone().detach(),
		 source_label_latent.clone().detach(),
										  langevin_steps=args.langevin_step, lr=args.langevin_lr, num_save=args.num_save)

		with torch.no_grad():
			for step in range(len(source_latent_q)):
				curr_latent = source_latent_q[step]
				curr_image = ae.dec(curr_latent)

				if args.color_space == 'RGB':
					LAB_curr_image = rgb_to_lab(curr_image)
				else:
					LAB_curr_image = curr_image

				if args.dataset == 'fundus':
					LAB_curr_image[:, 0:1, :, :] = L_channel_source_img

				if args.color_space == 'LAB':
					LAB_curr_image = denormalize_lab_tensor(LAB_curr_image)
					
				curr_image = lab_to_rgb(LAB_curr_image)
				
				curr_mask = torch.max(source_label) - source_label

				for k in range(curr_image.shape[0]):
					utils.save_image(curr_image[k], os.path.join(save_dir, 'image', f'{i}_{(step+1) * args.num_save}_{k}.png'), padding=0, normalize=True, range=(-1, 1), nrow=1)
					utils.save_image(curr_mask[k], os.path.join(save_dir, 'mask', f'{i}_{(step+1) * args.num_save}_{k}.png'), padding=0, normalize=True, range=(-1, 1), nrow=1)

if __name__ == "__main__":
	parser = argparse.ArgumentParser()

	parser.add_argument("--log_path", type=str, default='results')
	parser.add_argument("--n_samples", type=int, default=3_000_000)
	parser.add_argument("--n_gpu", type=int, default=1)
	parser.add_argument("--data_root", type=str)
	parser.add_argument("--ae_ckpt", type=str, default=None)
	parser.add_argument("--ebm_ckpt", type=str, default=None)

	# EBM Optimize
	parser.add_argument("--beta1", type=float, default=0.5)
	parser.add_argument("--beta2", type=float, default=0.999)
	parser.add_argument("--batch_size", type=int, default=16)
	parser.add_argument("--lr", type=float, default=0.0025)

	parser.add_argument("--seed", type=int, help="seed number")

	parser.add_argument("--l2", action="store_true")
	parser.add_argument("--refine", action='store_true')
	parser.add_argument("--noise", action='store_true')

	# Langevin
	parser.add_argument("--langevin_step", type=int, default=20)
	parser.add_argument("--langevin_lr", type=float, default=1.0)

	# Architecture
	parser.add_argument("--attention", action='store_true', help='if use attention')
	parser.add_argument("--cam", action='store_true', help='if use cam')
	parser.add_argument("--sn", action='store_true', help='if use spectral norm')
	parser.add_argument("--blur", action='store_true', help='if add blur')
	parser.add_argument("--embed_dim", type=int, default=128, help="latent dimension (depth)")
	parser.add_argument("--n_embed", type=int, default=512, help="number of embeddings in the codebook")
	parser.add_argument("--channel_mul", type=int, default=2, help="channel multipliers")

	# Dataset
	parser.add_argument("--dataset", type=str, default='fundus', choices=['fundus', 'prostate'])
	parser.add_argument("--source", type=str, default='cat')
	parser.add_argument("--target", type=str, default='dog')

	parser.add_argument("--suffix", type=str)

	parser.add_argument("--num_save", type=int, default=5)
	parser.add_argument("--color_space", type=str, choices=['RGB', 'LAB'], default='RGB')
	parser.add_argument("--expt_name", type=str, default='default')

	# Data augmentation

	args = parser.parse_args()

	if args.dataset == 'fundus':
		args.src_split_id = [int(d) for d in args.source]
		args.trg_split_id = [int(d) for d in args.target]

	args.data_path = os.path.join(args.data_root, args.dataset)

	if args.seed is not None:
		random.seed(args.seed)
		torch.manual_seed(args.seed)
		cudnn.deterministic = True
		warnings.warn('You have chosen to seed training. '
					  'This will turn on the CUDNN deterministic setting, '
					  'which can slow down your training considerably! '
					  'You may see unexpected behavior when restarting '
					  'from checkpoints.')

	if args.ae_ckpt is None:
		args.ae_ckpt = os.path.join(os.path.join("./checkpoint", args.dataset), "/vqvae.pt")
	assert os.path.isfile(args.ae_ckpt), "input a valid ckpt"

	prefix = args.dataset + "-" + args.source + '2' + args.target
	save_path = os.path.join(args.log_path, prefix)
	if not os.path.exists(save_path):
		os.makedirs(save_path)

	suffix = "-".join([item for item in [
		"convert",
		"ls%d" % (args.langevin_step),
		"llr%.4f" % (args.langevin_lr),
		"lr%.4f" % (args.lr),
		"embed%d" % (args.embed_dim),
		"nembed%d" % (args.n_embed),
		"attn" if args.attention else None,
		"noise" if args.noise else None,
		"cam" if args.cam else None,
		"sn" if args.sn else None,
		"l2" if args.l2 else None,
		"chm%d" % args.channel_mul,
		"beta%.1f_%.3f" % (args.beta1, args.beta2),
		# "ada" if args.ada else None,
		"blur" if args.blur else None,
		"%s"%(args.suffix) if args.suffix else None
	] if item is not None])
	args.run_dir = _create_run_dir_local(save_path, suffix)
	_copy_dir(['adapt.py', 'ebm.py', 'model'], args.run_dir)



	sys.stdout = Logger(os.path.join(args.run_dir, 'log.txt'))
	print(args)
	main(args)

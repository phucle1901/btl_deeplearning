import os

import pandas as pd
import torch
import torch.nn.functional as F
from PIL import Image
from torch.optim import Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from tqdm import tqdm

from rcdnet import RCDNet
from utils import parse_args, RainDataset, rgb_to_y, psnr, ssim


def train_loop(net, data_loader, n_iter):
    net.train()
    total_loss, total_num, train_bar = 0.0, 0, tqdm(data_loader, initial=1, dynamic_ncols=True)
    for rain, norain, name in train_bar:
        rain, norain = rain.cuda(), norain.cuda()
        b_0, list_b, list_r = net(rain)
        loss_bs = torch.stack([F.mse_loss(list_b[i], norain) for i in range(args.num_stage)]).sum()
        loss_rs = torch.stack([F.mse_loss(list_r[i], rain - norain) for i in range(args.num_stage)]).sum()
        loss_b = F.mse_loss(list_b[-1], norain)
        loss_r = F.mse_loss(list_r[-1], rain - norain)
        loss_b0 = F.mse_loss(b_0, norain)
        loss = 0.1 * loss_b0 + 0.1 * loss_bs + loss_b + 0.1 * loss_rs + 0.9 * loss_r

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total_num += rain.size(0)
        total_loss += loss.item() * rain.size(0)
        train_bar.set_description('Train Epoch: [{}/{}] Loss: {:.4f}'
                                  .format(n_iter, args.num_iter+start_epoch, total_loss / total_num))
    return total_loss / total_num


def test_loop(net, data_loader, n_iter):
    net.eval()
    total_psnr, total_ssim, count = 0.0, 0.0, 0
    with torch.no_grad():
        test_bar = tqdm(data_loader, initial=1, dynamic_ncols=True)
        for rain, norain, name in test_bar:
            rain, norain = rain.cuda(), norain.cuda()
            b_0, list_b, list_r = net(rain)
            out = torch.clamp(list_b[-1], 0, 255).byte()
            # computer the metrics with Y channel and double precision
            y, gt = rgb_to_y(out.double()), rgb_to_y(norain.double())
            current_psnr, current_ssim = psnr(y, gt), ssim(y, gt)
            total_psnr += current_psnr.item()
            total_ssim += current_ssim.item()
            count += 1
            save_path = '{}/{}/{}'.format(args.save_path, args.data_name, name[0])
            if not os.path.exists(os.path.dirname(save_path)):
                os.makedirs(os.path.dirname(save_path))
            Image.fromarray(out.squeeze(dim=0).permute(1, 2, 0).cpu().numpy()).save(save_path)
            test_bar.set_description('Test Epoch: [{}/{}] PSNR: {:.4f} SSIM: {:.4f}'
                                     .format(n_iter, 1 if args.test_only=="true" else (args.num_iter+start_epoch),
                                             total_psnr / count, total_ssim / count))
    return total_psnr / count, total_ssim / count


def save_loop(net, data_loader, n_iter):
    global best_psnr, best_ssim
    val_psnr, val_ssim = test_loop(net, data_loader, n_iter)
    results['PSNR'].append('{:.4f}'.format(val_psnr))
    results['SSIM'].append('{:.4f}'.format(val_ssim))
    # save statistics
    data_frame = pd.DataFrame(data=results)
    data_frame.to_csv('{}/{}.csv'.format(args.save_path, args.data_name), index_label='Epoch', float_format='%.4f')
    if val_psnr > best_psnr and val_ssim > best_ssim:
        best_psnr, best_ssim = val_psnr, val_ssim
        with open('{}/{}.txt'.format(args.save_path, args.data_name), 'w') as f:
            f.write('Epoch: {} PSNR:{:.2f} SSIM:{:.4f}'.format(n_iter, best_psnr, best_ssim))
    torch.save({
        'epoch': n_iter,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': lr_scheduler.state_dict()
    }, f'{args.save_path}/{args.data_name}_{n_iter}.pth')



if __name__ == '__main__':
    args = parse_args()
    test_dataset = RainDataset(args.data_path, args.data_name, 'test')
    test_loader = DataLoader(test_dataset, batch_size=1, shuffle=False, num_workers=args.workers)
    train_dataset = RainDataset(args.data_path, args.data_name, 'train', args.patch_size, args.batch_size * 1500)
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=args.workers)
    
    results, best_psnr, best_ssim = {'PSNR': [], 'SSIM': []}, 0.0, 0.0
    model = RCDNet(args.num_map, args.num_channel, args.num_block, args.num_stage).cuda()
    optimizer = Adam(model.parameters(), lr=args.lr)
    lr_scheduler = MultiStepLR(optimizer, milestones=args.milestone, gamma=0.2)
    if args.test_only=="false":
        results['Loss'] = []

    baseline = []
    for rain, norain, _ in test_loader:
        baseline.append(
            ssim(rgb_to_y(rain.double()), rgb_to_y(norain.double())).item()
        )
    print(sum(baseline)/len(baseline))
    start_epoch=0
    if args.model_file and (args.test_only=="false"):  #train tiep
        print("dang chay phan train tiep")
        ckpt = torch.load(args.model_file)
        model.load_state_dict(ckpt['model_state_dict'])
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        lr_scheduler.load_state_dict(ckpt['scheduler_state_dict'])
        start_epoch = ckpt['epoch'] + 1 
        for epoch in range(start_epoch, start_epoch+args.num_iter ):
            train_loss = train_loop(model, train_loader, epoch)
            results['Loss'].append('{:.4f}'.format(train_loss))
            lr_scheduler.step()
            save_loop(model, test_loader, epoch)
            
    elif args.model_file and (args.test_only=="true"): #test
        ckpt = torch.load(args.model_file)
        model.load_state_dict(ckpt['model_state_dict'])
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        lr_scheduler.load_state_dict(ckpt['scheduler_state_dict'])
        print("dang chay phan test")
        save_loop(model, test_loader, 1)

    else:  #train lan dau'
        print("train lan dau")
        for epoch in range(1, args.num_iter + 1):
            train_loss = train_loop(model, train_loader, epoch)
            results['Loss'].append('{:.4f}'.format(train_loss))
            lr_scheduler.step()
            save_loop(model, test_loader, epoch)


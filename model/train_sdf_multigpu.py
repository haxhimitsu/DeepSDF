import torch
import model.model_sdf as sdf_model
import torch.optim as optim
import data_making.dataset_sdf as dataset
from torch.utils.data import random_split
from torch.utils.data import DataLoader
import argparse
import results.runs_sdf as runs
from utils.utils_deepsdf import SDFLoss_multishape
import os
from datetime import datetime
import numpy as np
import time
from utils import utils_deepsdf
import results
from torch.utils.tensorboard import SummaryWriter
import json
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.multiprocessing as mp

GPU = True if torch.cuda.device_count() >= 1 else False

#device = torch.device("cuda:0" if (torch.cuda.is_available() and not MULTI_GPU) else "cpu")

if torch.cuda.is_available():
    print(torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count()))

# Method to setup distributed training
def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group("nccl", rank=rank, world_size=world_size)

class Trainer():
    def __init__(self, args):
        self.args = args

    def __call__(self):
        torch.seed(self.args.seed)

        # directories
        self.timestamp_run = datetime.now().strftime('%d_%m_%H%M%S')   # timestamp to use for logging data
        self.runs_dir = os.path.dirname(runs.__file__)               # directory fo all runs
        self.run_dir = os.path.join(self.runs_dir, self.timestamp_run)  # directory for this run
        if not os.path.exists(self.run_dir):
            os.makedirs(self.run_dir)
        
        # Logging
        self.writer = SummaryWriter(log_dir=self.run_dir)
        self.log_path = os.path.join(self.run_dir, 'settings.txt')
        args_dict = vars(self.args)  # convert args to dict to write them as json
        args_dict['gpus'] = torch.cuda.device_count() if torch.cuda.is_available() else 0
        with open(self.log_path, mode='a') as log:
            log.write('Settings:\n')
            log.write(json.dumps(args_dict).replace(', ', ',\n'))
            log.write('\n\n')

        # calculate num objects in samples_dictionary, wich is the number of keys
        samples_dict_path = os.path.join(os.path.dirname(results.__file__), f'samples_dict_{args.dataset}.npy')
        samples_dict = np.load(samples_dict_path, allow_pickle=True).item()

        world_size = torch.cuda.device_count() if torch.cuda.is_available() else 0
        
        mp.spawn(
            self.main,
            args=(world_size, samples_dict),
            nprocs=world_size
        )


        # # instantiate model and optimisers
        # self.model = sdf_model.SDFModelMulti(self.args.num_layers, self.args.no_skip_connections, input_dim=self.args.latent_size + 3, inner_dim=self.args.inner_dim).float().to(device)
        # if self.args.pretrained:
        #     self.model.load_state_dict(torch.load(self.args.pretrain_weights, map_location=device))
        # self.optimizer_model = optim.Adam(self.model.parameters(), lr=self.args.lr_model, weight_decay=0)
        # # generate a unique random latent code for each shape
        # self.latent_codes, self.dict_latent_codes = utils_deepsdf.generate_latent_codes(self.args.latent_size, samples_dict)
        # self.optimizer_latent = optim.Adam([self.latent_codes], lr=self.args.lr_latent, weight_decay=0)

        # if self.args.lr_scheduler:
        #     self.scheduler_model =  torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer_model, mode='min', factor=self.args.lr_multiplier, patience=self.args.patience, threshold=0.0001, threshold_mode='rel')
        #     self.scheduler_latent =  torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer_latent, mode='min', factor=self.args.lr_multiplier, patience=self.args.patience, threshold=0.0001, threshold_mode='rel')
            
        # # get data
        # train_loader, val_loader = self.get_loaders()
        # self.results = {
        #     'train':  {'loss': [], 'latent_codes': [], 'best_latent_codes' : []},
        #     'val':    {'loss': []}
        # }
        # #utils.model_graph_to_tensorboard(train_loader, self.model, self.writer, self.generate_xy)

        # self.running_steps = 0   # counter for latent codes tensorboard
        # best_loss = 10000000000
        # start = time.time()
        # for epoch in range(self.args.epochs):
        #     print(f'============================ Epoch {epoch} ============================')
        #     self.epoch = epoch

        #     avg_train_loss = self.train(train_loader)

        #     self.results['train']['loss'].append(avg_train_loss)
        #     self.results['train']['latent_codes'].append(self.latent_codes.detach().cpu().numpy())

        #     with torch.no_grad():
        #         avg_val_loss = self.validate(val_loader)

        #         self.results['val']['loss'].append(avg_val_loss)

        #         if avg_val_loss < best_loss:
        #             best_loss = np.copy(avg_val_loss)
        #             best_weights = self.model.state_dict()
        #             best_latent_codes = self.latent_codes.detach().cpu().numpy()

        #         if self.args.lr_scheduler:
        #             self.scheduler_model.step(avg_val_loss)
        #             self.scheduler_latent.step(avg_val_loss)

        #             for param_group in self.optimizer_model.param_groups:
        #                 print(f"Learning rate (model): {param_group['lr']}")
        #             for param_group in self.optimizer_latent.param_groups:
        #                 print(f"Learning rate (latent): {param_group['lr']}")

        #             self.writer.add_scalar('Learning rate (model)', self.scheduler_model._last_lr[0], epoch)
        #             self.writer.add_scalar('Learning rate (latent)', self.scheduler_latent._last_lr[0], epoch)
            
        #     np.save(os.path.join(self.run_dir, 'results.npy'), self.results)
        #     torch.save(best_weights, os.path.join(self.run_dir, 'weights.pt'))
        #     self.results['train']['best_latent_codes'] = best_latent_codes
            
        # end = time.time()
        # print(f'Time elapsed: {end - start} s')

    def main(self, rank, world_size, samples_dict):
        self.rank = rank
        setup(rank, world_size)

        print(f'World size: {world_size}')
        
        device = torch.device(rank if torch.cuda.is_available() else 'cpu')

        print(f'Rank {rank}, device {device}')

        # instantiate model and optimisers
        self.model = sdf_model.SDFModelMulti(self.args.num_layers, self.args.no_skip_connections, input_dim=self.args.latent_size + 3, inner_dim=self.args.inner_dim).float().to(device)

        self.model = DDP(self.model, device_ids=[rank], output_device=rank, find_unused_parameters=True)

        if self.args.pretrained:
            self.model.load_state_dict(torch.load(self.args.pretrain_weights, map_location=device))

        self.optimizer_model = optim.Adam(self.model.parameters(), lr=self.args.lr_model, weight_decay=0)

        # generate a unique random latent code for each shape
        self.latent_codes, self.dict_latent_codes = utils_deepsdf.generate_latent_codes(self.args.latent_size, samples_dict)
        self.optimizer_latent = optim.Adam([self.latent_codes], lr=self.args.lr_latent, weight_decay=0)

        if self.args.lr_scheduler:
            self.scheduler_model =  torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer_model, mode='min', factor=self.args.lr_multiplier, patience=self.args.patience, threshold=0.0001, threshold_mode='rel')
            self.scheduler_latent =  torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer_latent, mode='min', factor=self.args.lr_multiplier, patience=self.args.patience, threshold=0.0001, threshold_mode='rel')
            
        # get data
        train_loader, val_loader = self.get_loaders(world_size, rank)
        self.results = {
            'train':  {'loss': [], 'latent_codes': [], 'best_latent_codes' : []},
            'val':    {'loss': []}
        }
        #utils.model_graph_to_tensorboard(train_loader, self.model, self.writer, self.generate_xy)

        self.running_steps = 0   # counter for latent codes tensorboard
        best_loss = 10000000000
        start = time.time()
        for epoch in range(self.args.epochs):
            print(f'============================ Epoch {epoch} ============================')
            self.epoch = epoch

            if GPU:
                train_loader.sampler.set_epoch(epoch)
                val_loader.set_epoch(epoch)

            avg_train_loss = self.train(train_loader, device)

            self.results['train']['loss'].append(avg_train_loss)
            self.results['train']['latent_codes'].append(self.latent_codes.detach().cpu().numpy())

            with torch.no_grad():
                avg_val_loss = self.validate(val_loader, device)

                self.results['val']['loss'].append(avg_val_loss)

                if avg_val_loss < best_loss:
                    best_loss = np.copy(avg_val_loss)
                    best_weights = self.model.state_dict()
                    best_latent_codes = self.latent_codes.detach().cpu().numpy()

                if self.args.lr_scheduler:
                    self.scheduler_model.step(avg_val_loss)
                    self.scheduler_latent.step(avg_val_loss)

                    for param_group in self.optimizer_model.param_groups:
                        print(f"Learning rate (model): {param_group['lr']}")
                    for param_group in self.optimizer_latent.param_groups:
                        print(f"Learning rate (latent): {param_group['lr']}")

                    self.writer.add_scalar('Learning rate (model)', self.scheduler_model._last_lr[0], epoch)
                    self.writer.add_scalar('Learning rate (latent)', self.scheduler_latent._last_lr[0], epoch)
            
            np.save(os.path.join(self.run_dir, 'results.npy'), self.results)
            torch.save(best_weights, os.path.join(self.run_dir, 'weights.pt'))
            self.results['train']['best_latent_codes'] = best_latent_codes
            
        end = time.time()
        print(f'Time elapsed: {end - start} s')

        if GPU:
            dist.destroy_process_group()

    def get_loaders(self, world_size, rank):
        data = dataset.SDFDataset(self.args.dataset)
        train_size = int(0.8 * len(data))
        val_size = len(data) - train_size
        train_data, val_data = random_split(data, [train_size, val_size])

        # Define samplers
        train_sampler = DistributedSampler(train_data, num_replicas=world_size, rank=rank)
        val_sampler = DistributedSampler(val_data, num_replicas=world_size, rank=rank)

        # Define loaders
        train_loader = DataLoader(
            train_data,
            batch_size=self.args.batch_size,
            shuffle=True,
            drop_last=True,
            sampler=train_sampler,
            num_workers=0,
            pin_memory=False
        )
        val_loader = DataLoader(
            val_data,
            batch_size=self.args.batch_size,
            shuffle=False,
            drop_last=True,
            sampler=val_sampler,
            num_workers=0,
            pin_memory=False
        )
        return train_loader, val_loader

    def get_latent_proportions(self, latent_codes_indices_batch):
        """
        The gradient is averaged across the entire batch, but every latent code only appears in a fraction of this batch.
        Therefore, the gradients wrt latent vectors are underestimated - because each latent vector only contributes
        to a proportion of the average gradient computed across the entire batch size.
        Solution: compute the proportion of samples per latent classes, and ajust the gradient accordingly.
        """
        unique_latent_indices_batch, counts = latent_codes_indices_batch.unique(return_counts=True)
        return unique_latent_indices_batch, counts 

    def generate_xy(self, batch, device):
        """
        Combine latent code and coordinates.
        Return:
            - x: latent codes + coordinates, torch tensor shape (batch_size, latent_size + 3)
            - y: ground truth sdf, shape (batch_size, 1)
            - latent_codes_indices_batch: all latent class indices per sample, shape (batch size, 1).
                                            e.g. [[2], [2], [1], ..] eaning the batch contains the 2nd, 2nd, 1st latent code
            - latent_batch_codes: all latent codes per sample, shape (batch_size, latent_size)
        Return ground truth as y, and the latent codes for this batch.
        """
        latent_classes_batch = batch[0][:, 0].view(-1, 1)               # shape (batch_size, 1)
        coords = batch[0][:, 1:]                                  # shape (batch_size, 3)
        latent_codes_indices_batch = torch.tensor(
                [self.dict_latent_codes[int(latent_class)] for latent_class in latent_classes_batch],
                dtype=torch.int64
            ).to(device)
        latent_codes_batch = self.latent_codes[latent_codes_indices_batch]    # shape (batch_size, 128)
        x = torch.hstack((latent_codes_batch, coords))                  # shape (batch_size, 131)
        y = batch[1]     # (batch_size, 1)
        if args.clamp:
            y = torch.clamp(y, -args.clamp_value, args.clamp_value)
        return x, y, latent_codes_indices_batch, latent_codes_batch
    
    def train(self, train_loader, device):
        total_loss = 0.0
        iterations = 0.0
        self.model.train()
        for batch in train_loader:
            # batch[0]: [class, x, y, z], shape: (batch_size, 4)
            # batch[1]: [sdf], shape: (batch size)
            iterations += 1.0
            self.running_steps += 1   # counter for latent codes tensorboard

            self.optimizer_model.zero_grad()
            self.optimizer_latent.zero_grad()

            x, y, latent_codes_indices_batch, latent_codes_batch = self.generate_xy(batch, device)
            #unique_latent_indices_batch, counts = self.get_latent_proportions(latent_codes_indices_batch)

            predictions = self.model(x)  # (batch_size, 1)
            if args.clamp:
                predictions = torch.clamp(predictions, -args.clamp_value, args.clamp_value)
            
            loss_value, l1, l2 = self.args.loss_multiplier * SDFLoss_multishape(y, predictions, x[:, :self.args.latent_size], sigma=self.args.sigma_regulariser)
            loss_value.backward()       

            print(f'Rank {self.rank}, Loss value {loss_value.item()}')

            self.optimizer_latent.step()
            self.optimizer_model.step()
            total_loss += loss_value.data.cpu().numpy()  

            if self.args.latent_to_tensorboard:
                utils_deepsdf.latent_to_tensorboard(self.writer, self.running_steps, self.latent_codes)

        avg_train_loss = total_loss/iterations
        print(f'Training: loss {avg_train_loss}')
        self.writer.add_scalar('Training loss', avg_train_loss, self.epoch)

        if self.args.weights_to_tensorboard:
            utils_deepsdf.weight_to_tensorboard(self.writer, self.epoch, self.model)

        return avg_train_loss

    def validate(self, val_loader, device):
        total_loss = 0.0
        iterations = 0.0
        self.model.eval()

        for batch in val_loader:
            # batch[0]: [class, x, y, z], shape: (batch_size, 4)
            # batch[1]: [sdf], shape: (batch size)
            iterations += 1.0            

            x, y, _, latent_codes_batch = self.generate_xy(batch)

            predictions = self.model(x)  # (batch_size, 1)
            if args.clamp:
                predictions = torch.clamp(predictions, -args.clamp_value, args.clamp_value)

            loss_value, l1, l2 = self.args.loss_multiplier * SDFLoss_multishape(y, predictions, latent_codes_batch, self.args.sigma_regulariser)          
            total_loss += loss_value.data.cpu().numpy()   

        avg_val_loss = total_loss/iterations
        print(f'Validation: loss {avg_val_loss}')
        self.writer.add_scalar('Validation loss', avg_val_loss, self.epoch)

        return avg_val_loss

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default='ShapeNetCore', type=str, help="Dataset used: 'ShapeNetCore' or 'PartNetMobility'"
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Setting for the random seed"
    )
    parser.add_argument(
        "--epochs", type=int, default=500, help="Number of epochs to use"
    )
    parser.add_argument(
        "--lr_model", type=float, default=0.00001, help="Initial learning rate (model)"
    )
    parser.add_argument(
        "--lr_latent", type=float, default=0.001, help="Initial learning rate (latent vector)"
    )
    parser.add_argument(
        "--batch_size", type=int, default=500, help="Size of the batch"
    )
    parser.add_argument(
        "--latent_size", type=int, default=128, help="Size of the latent size"
    )
    parser.add_argument(
        "--sigma_regulariser", type=float, default=0.01, help="Sigma value for the regulariser in the loss function"
    )
    parser.add_argument(
        "--loss_multiplier", type=float, default=1, help="Loss multiplier"
    )
    parser.add_argument(
        "--weights_to_tensorboard", default=False, action='store_true', help="Store model parameters for visualisation on tensorboard"
    )
    parser.add_argument(
        "--latent_to_tensorboard", default=False, action='store_true', help="Store latent codes for visualisation on tensorboard"
    )
    parser.add_argument(
        "--lr_multiplier", type=float, default=0.5, help="Multiplier for the learning rate scheduling"
    )  
    parser.add_argument(
        "--patience", type=int, default=20, help="Patience for the learning rate scheduling"
    )  
    parser.add_argument(
        "--lr_scheduler", default=False, action='store_true', help="Turn on lr_scheduler"
    )
    parser.add_argument(
        "--num_layers", type=int, default=8, help="Num network layers"
    )    
    parser.add_argument(
        "--no_skip_connections", default=False, action='store_true', help="Do not skip connections"
    )   
    parser.add_argument(
        "--clamp", default=False, action='store_true', help="Clip the network prediction"
    )
    parser.add_argument(
        "--clamp_value", type=float, default=0.1, help="Value for clipping"
    )
    parser.add_argument(
        "--pretrained", default=False, action='store_true', help="Use pretrain weights"
    )
    parser.add_argument(
        "--pretrain_weights", type=str, default='', help="Path to pretrain weights"
    )
    parser.add_argument(
        "--inner_dim", type=int, default=512, help="Inner dimensions of the network"
    )
    args = parser.parse_args()

    trainer = Trainer(args)
    trainer()
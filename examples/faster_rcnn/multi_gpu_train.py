#coding: 'utf-8'

"""
faster_rcnn_chainercv
multi_gpu_train

created by Kazunari on 2018/06/26 
"""

from __future__ import division

import argparse
import numpy as np

import chainer
from chainer.datasets import ConcatenatedDataset
from chainer.datasets import TransformDataset
from chainer import training
from chainer.training import extensions
from chainer.training.triggers import ManualScheduleTrigger

from chainercv.datasets import voc_bbox_label_names
from chainercv.datasets import VOCBboxDataset
from chainercv.extensions import DetectionVOCEvaluator
from chainercv.links import FasterRCNNVGG16
from chainercv.links.model.faster_rcnn import FasterRCNNTrainChain
from chainercv import transforms

import chainermn


class Transform(object):

    def __init__(self, faster_rcnn):
        self.faster_rcnn = faster_rcnn

    def __call__(self, in_data):
        img, bbox, label = in_data
        _, H, W = img.shape
        img = self.faster_rcnn.prepare(img)
        _, o_H, o_W = img.shape
        scale = o_H / H
        bbox = transforms.resize_bbox(bbox, (H, W), (o_H, o_W))

        # horizontally flip
        img, params = transforms.random_flip(
            img, x_random=True, return_param=True)
        bbox = transforms.flip_bbox(
            bbox, (o_H, o_W), x_flip=params['x_flip'])

        return img, bbox, label, scale


def main():
    parser = argparse.ArgumentParser(
        description='ChainerCV training example: Faster R-CNN')
    parser.add_argument('--dataset', choices=('voc07', 'voc0712'),
                        help='The dataset to use: VOC07, VOC07+12',
                        default='voc07')
    parser.add_argument('--gpu', '-g', type=int, default=-1)
    parser.add_argument('--lr', '-l', type=float, default=1e-3)
    parser.add_argument('--out', '-o', default='result',
                        help='Output directory')
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--step_size', '-ss', type=int, default=50000)
    parser.add_argument('--iteration', '-i', type=int, default=70000)
    args = parser.parse_args()

    np.random.seed(args.seed)

    if args.dataset == 'voc07':
        train_data = VOCBboxDataset(split='trainval', year='2007')
    elif args.dataset == 'voc0712':
        train_data = ConcatenatedDataset(
            VOCBboxDataset(year='2007', split='trainval'),
            VOCBboxDataset(year='2012', split='trainval'))

    comm = chainermn.create_communicator('hierarchical')
    device = comm.intra_rank

    n_node = comm.intra_rank
    n_gpu = comm.size
    chainer.cuda.get_device_from_id(device).use()

    total_batch_size = n_gpu

    args.lr = args.lr * total_batch_size

    test_data = VOCBboxDataset(split='test', year='2007',
                               use_difficult=True, return_difficult=True)
    faster_rcnn = FasterRCNNVGG16(n_fg_class=len(voc_bbox_label_names),
                                  pretrained_model='imagenet')
    faster_rcnn.use_preset('evaluate')
    model = FasterRCNNTrainChain(faster_rcnn)
    model.to_gpu()

    optimizer = chainermn.create_multi_node_optimizer(chainer.optimizers.MomentumSGD(lr=args.lr, momentum=0.9), comm)

    optimizer.setup(model)
    optimizer.add_hook(chainer.optimizer_hooks.WeightDecay(rate=0.0005))

    train_data = TransformDataset(train_data, Transform(faster_rcnn))

    if comm.rank != 0:
        train_data = None
        test_data = None
    train_data = chainermn.scatter_dataset(train_data, comm, shuffle=True)
    test_data = chainermn.scatter_dataset(test_data, comm)

    train_iter = chainer.iterators.SerialIterator(
        train_data, batch_size=1
    )
    test_iter = chainer.iterators.SerialIterator(
        test_data, batch_size=1, repeat=False, shuffle=False)

    updater = chainer.training.updaters.StandardUpdater(
        train_iter, optimizer, device=device)

    trainer = training.Trainer(
        updater, (args.iteration, 'iteration'), out=args.out)

    log_interval = 20, 'iteration'
    plot_interval = 3000, 'iteration'
    print_interval = 20, 'iteration'

    evaluator = DetectionVOCEvaluator(
        test_iter,
        model,
        device=device,
        use_07_metric=True,
        label_names=voc_bbox_label_names
    )

    evaluator = chainermn.create_multi_node_evaluator(evaluator, comm)
    trainer.extend(evaluator, trigger=ManualScheduleTrigger([args.step_size, args.iteration], 'iteration'))

    trainer.extend(extensions.ExponentialShift('lr', 0.1),
                   trigger=(args.step_size, 'iteration'))

    if comm.rank == 0:
        trainer.extend(
            extensions.snapshot_object(model.faster_rcnn, 'snapshot_model.npz'),
            trigger=(args.iteration, 'iteration'))

        trainer.extend(chainer.training.extensions.observe_lr(),
                       trigger=log_interval)
        trainer.extend(extensions.LogReport(trigger=log_interval))
        trainer.extend(extensions.PrintReport(
            ['iteration', 'epoch', 'elapsed_time', 'lr',
             'main/loss',
             'main/roi_loc_loss',
             'main/roi_cls_loss',
             'main/rpn_loc_loss',
             'main/rpn_cls_loss',
             'validation/main/map',
             ]), trigger=print_interval)
        trainer.extend(extensions.ProgressBar(update_interval=10))

        if extensions.PlotReport.available():
            trainer.extend(
                extensions.PlotReport(
                    ['main/loss'],
                    file_name='loss.png', trigger=plot_interval
                ),
                trigger=plot_interval
            )

        trainer.extend(extensions.dump_graph('main/loss'))

    trainer.run()


if __name__ == '__main__':
    main()

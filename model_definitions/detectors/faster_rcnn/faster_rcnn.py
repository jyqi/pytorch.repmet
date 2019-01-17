
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from torch.autograd import Variable
from rpn.rpn import RPN

from roi_layers import ROIAlign, ROIPool

from rpn.proposal_target_layer_cascade import _ProposalTargetLayer

from utils.functions import _smooth_l1_loss


class FasterRCNN(nn.Module):
    """ faster RCNN """
    def __init__(self,
                 output_size,
                 config):

        super(FasterRCNN, self).__init__()
        self.output_size = output_size

        self.class_agnostic = config.model.class_agnostic
        self.backbone_type = config.model.backbone.type
        self.pooling_mode = config.model.pooling_mode
        self.truncated = config.train.truncated

        # Get backbone network
        self.backbone_base, self.backbone_top, dout_base_model = self._init_backbone(type=config.model.backbone.type,
                                                                                     n_layers=config.model.backbone.n_layers,
                                                                                     pretrained=config.model.backbone.pretrained,
                                                                                     fixed_blocks=config.model.backbone.resnet_fixed_blocks)

        # Set up the classification layers
        if config.model.backbone.type == 'resnet':
            hid_dim = 2048
        elif config.model.backbone.type == 'vgg':
            hid_dim = 4096

        self.RCNN_cls_score = nn.Linear(hid_dim, self.output_size)
        if self.class_agnostic:
            self.RCNN_bbox_pred = nn.Linear(hid_dim, 4)
        else:
            self.RCNN_bbox_pred = nn.Linear(hid_dim, 4 * self.output_size)

        # define rpn
        self.RCNN_rpn = RPN(config, dout_base_model,
                            anchor_scales=config.model.rpn.anchor_scales,
                            anchor_ratios=config.model.rpn.anchor_ratios,
                            feat_stride=config.model.rpn.feat_stride)

        self.RCNN_proposal_target = _ProposalTargetLayer(config, self.output_size)

        self.RCNN_roi_pool = ROIPool((config.model.pooling_size, config.model.pooling_size), 1.0/16.0)
        self.RCNN_roi_align = ROIAlign((config.model.pooling_size, config.model.pooling_size), 1.0/16.0, 0)

        self._init_weights()

    def forward(self, im_data, im_info, gt_boxes, num_boxes):
        batch_size = im_data.size(0)

        im_info = im_info.data
        gt_boxes = gt_boxes.data
        num_boxes = num_boxes.data

        # feed image data to base model to obtain base feature map
        base_feat = self.backbone_base(im_data)

        # feed base feature map tp RPN to obtain rois
        rois, rpn_loss_cls, rpn_loss_bbox = self.RCNN_rpn(base_feat, im_info, gt_boxes, num_boxes)

        # if it is training phrase, then use ground truth boxes for refining
        if self.training:
            roi_data = self.RCNN_proposal_target(rois, gt_boxes, num_boxes)
            rois, rois_label, rois_target, rois_inside_ws, rois_outside_ws = roi_data

            rois_label = Variable(rois_label.view(-1).long())
            rois_target = Variable(rois_target.view(-1, rois_target.size(2)))
            rois_inside_ws = Variable(rois_inside_ws.view(-1, rois_inside_ws.size(2)))
            rois_outside_ws = Variable(rois_outside_ws.view(-1, rois_outside_ws.size(2)))
        else:
            rois_label = None
            rois_target = None
            rois_inside_ws = None
            rois_outside_ws = None
            rpn_loss_cls = 0
            rpn_loss_bbox = 0

        rois = Variable(rois)
        # do roi pooling based on predicted rois

        if self.pooling_mode == 'align':
            pooled_feat = self.RCNN_roi_align(base_feat, rois.view(-1, 5))
        elif self.pooling_mode == 'pool':
            pooled_feat = self.RCNN_roi_pool(base_feat, rois.view(-1, 5))

        # feed pooled features to top model
        pooled_feat = self._head_to_tail(pooled_feat)

        # compute bbox offset
        bbox_pred = self.RCNN_bbox_pred(pooled_feat)
        if self.training and not self.class_agnostic:
            # select the corresponding columns according to roi labels
            bbox_pred_view = bbox_pred.view(bbox_pred.size(0), int(bbox_pred.size(1) / 4), 4)
            bbox_pred_select = torch.gather(bbox_pred_view, 1, rois_label.view(rois_label.size(0), 1, 1).expand(rois_label.size(0), 1, 4))
            bbox_pred = bbox_pred_select.squeeze(1)

        # compute object classification probability
        cls_score = self.RCNN_cls_score(pooled_feat)
        cls_prob = F.softmax(cls_score, 1)

        RCNN_loss_cls = 0
        RCNN_loss_bbox = 0

        if self.training:
            # classification loss
            RCNN_loss_cls = F.cross_entropy(cls_score, rois_label)

            # bounding box regression L1 loss
            RCNN_loss_bbox = _smooth_l1_loss(bbox_pred, rois_target, rois_inside_ws, rois_outside_ws)


        cls_prob = cls_prob.view(batch_size, rois.size(1), -1)
        bbox_pred = bbox_pred.view(batch_size, rois.size(1), -1)

        return rois, cls_prob, bbox_pred, rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox, rois_label

    def _init_weights(self):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initalizer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean) # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
                m.bias.data.zero_()

        normal_init(self.RCNN_rpn.RPN_Conv, 0, 0.01, self.truncated)
        normal_init(self.RCNN_rpn.RPN_cls_score, 0, 0.01, self.truncated)
        normal_init(self.RCNN_rpn.RPN_bbox_pred, 0, 0.01, self.truncated)
        normal_init(self.RCNN_cls_score, 0, 0.01, self.truncated)
        normal_init(self.RCNN_bbox_pred, 0, 0.001, self.truncated)


    def _init_backbone(self, type='resnet', n_layers=101, pretrained=False, fixed_blocks=None):

        # initialise the backbone and load weights
        if pretrained == 'caffe':
            if type == 'resnet' and n_layers == 101:
                backbone = models.resnet101(pretrained=False)
                model_path = 'data/pretrained_model/resnet101_caffe.pth'
            elif type == 'vgg' and n_layers == 16:
                backbone = models.vgg16(pretrained=False)
                model_path = 'data/pretrained_model/vgg16_caffe.pth'
            else:
                ValueError

            print("Loading pretrained weights from %s" % model_path)
            state_dict = torch.load(model_path)
            backbone.load_state_dict({k: v for k, v in state_dict.items() if k in backbone.state_dict()})

        elif type == 'resnet' and n_layers == 101:
            backbone = models.resnet101(pretrained=pretrained)

        if type == 'resnet':
            # Make the backbone components
            backbone_base = nn.Sequential(backbone.conv1, backbone.bn1, backbone.relu,
                                          backbone.maxpool, backbone.layer1, backbone.layer2, backbone.layer3)

            backbone_top = nn.Sequential(backbone.layer4)

            dout_base_model = 1024

            # Fix the parameters of the backbone
            for p in backbone_base[0].parameters(): p.requires_grad = False
            for p in backbone_base[1].parameters(): p.requires_grad = False

            assert (0 <= fixed_blocks < 4)
            if fixed_blocks >= 3:
                for p in backbone_base[6].parameters(): p.requires_grad = False
            if fixed_blocks >= 2:
                for p in backbone_base[5].parameters(): p.requires_grad = False
            if fixed_blocks >= 1:
                for p in backbone_base[4].parameters(): p.requires_grad = False

            def set_bn_fix(m):
                classname = m.__class__.__name__
                if classname.find('BatchNorm') != -1:
                    for p in m.parameters(): p.requires_grad = False

            backbone_base.apply(set_bn_fix)
            backbone_top.apply(set_bn_fix)

        elif type == 'vgg':
            # Make the backbone components
            backbone_base = nn.Sequential(*list(backbone.features._modules.values())[:-1])
            backbone_top = nn.Sequential(*list(backbone.classifier._modules.values())[:-1])

            dout_base_model = 512

            # Fix the parameters of the backbone
            # Fix the layers before conv3:
            for layer in range(10):
                for p in backbone_base[layer].parameters(): p.requires_grad = False

        return backbone_base, backbone_top, dout_base_model

    def train(self, mode=True): # for resnet only
        # Override train so that the training mode is set as we want
        nn.Module.train(self, mode)
        if mode and self.backbone_type == 'resnet':
            # Set fixed blocks to be in eval mode
            self.backbone_base.eval()
            self.backbone_base[5].train()
            self.backbone_base[6].train()

            def set_bn_eval(m):
                classname = m.__class__.__name__
                if classname.find('BatchNorm') != -1:
                    m.eval()

            self.backbone_base.apply(set_bn_eval)
            self.backbone_top.apply(set_bn_eval)

    def _head_to_tail(self, pool5):

        if self.backbone_type == 'resnet':
            fc7 = self.backbone_top(pool5).mean(3).mean(2)

        elif self.backbone_type == 'vgg':
            pool5_flat = pool5.view(pool5.size(0), -1)
            fc7 = self.backbone_top(pool5_flat)

        return fc7

if __name__ == "__main__":
    # use this for debugging and checks
    from utils.debug import set_working_dir
    from config.config import config
    import matplotlib.pyplot as plt

    # set the working directory as appropriate
    set_working_dir()

    # load the dataset
    model = FasterRCNN(list(range(100)), config=config)
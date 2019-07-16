from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import torch
import numpy as np


class UResNet(torch.nn.Module):
    """
    UResNet

    For semantic segmentation, using sparse convolutions from SCN, but not the
    ready-made UNet from SCN library. The option `ghost` allows to train at the
    same time for semantic segmentation between N classes (e.g. particle types)
    and ghost points masking.

    Can also be used in a chain, for example stacking PPN layers on top.

    Configuration
    -------------
    num_strides : int
        Depth of UResNet, also corresponds to how many times we down/upsample.
    filters : int
        Number of filters in the first convolution of UResNet.
        Will increase linearly with depth.
    num_classes : int
        Should be number of classes (+1 if we include ghost points directly)
    data_dim : int
        Dimension 2 or 3
    spatial_size : int
        Size of the cube containing the data, e.g. 192, 512 or 768px.
    reps : int, optional
        Convolution block repetition factor
    kernel_size : int, optional
        Kernel size for the SC (sparse convolutions for down/upsample).
    features: int, optional
        How many features are given to the network initially.

    Returns
    -------
    list
        In order:
        - segmentation scores (N, num_classes)
        - feature maps of encoding path
        - feature maps of decoding path
    """

    def __init__(self, cfg, name="uresnet_clustering"):
        super(UResNet, self).__init__()
        import sparseconvnet as scn
        self._model_config = cfg['modules'][name]

        # Whether to compute ghost mask separately or not
        self._ghost = self._model_config.get('ghost', False)
        self._dimension = self._model_config.get('data_dim', 3)
        reps = self._model_config.get('reps', 2)  # Conv block repetition factor
        kernel_size = self._model_config.get('kernel_size', 2)
        num_strides = self._model_config.get('num_strides', 5)
        m = self._model_config.get('filters', 16)  # Unet number of features
        nInputFeatures = self._model_config.get('features', 1)
        spatial_size = self._model_config.get('spatial_size', 512)
        num_classes = self._model_config.get('num_classes', 5)
        self._N = self._model_config.get('num_cluster_conv', 0)

        nPlanes = [i*m for i in range(1, num_strides+1)]  # UNet number of features per level
        downsample = [kernel_size, 2]  # [filter size, filter stride]
        self.last = None
        leakiness = 0

        def block(m, a, b):  # ResNet style blocks
            m.add(scn.ConcatTable()
                  .add(scn.Identity() if a == b else scn.NetworkInNetwork(a, b, False))
                  .add(scn.Sequential()
                    .add(scn.BatchNormLeakyReLU(a, leakiness=leakiness))
                    .add(scn.SubmanifoldConvolution(self._dimension, a, b, 3, False))
                    .add(scn.BatchNormLeakyReLU(b, leakiness=leakiness))
                    .add(scn.SubmanifoldConvolution(self._dimension, b, b, 3, False)))
             ).add(scn.AddTable())

        self.input = scn.Sequential().add(
           scn.InputLayer(self._dimension, spatial_size, mode=3)).add(
           scn.SubmanifoldConvolution(self._dimension, nInputFeatures, m, 3, False)) # Kernel size 3, no bias
        self.concat = scn.JoinTable()
        # Encoding
        self.bn = scn.BatchNormLeakyReLU(nPlanes[0], leakiness=leakiness)
        self.encoding_block = scn.Sequential()
        self.encoding_conv = scn.Sequential()
        module = scn.Sequential()
        for i in range(num_strides):
            module = scn.Sequential()
            for _ in range(reps):
                block(module, nPlanes[i], nPlanes[i])
            self.encoding_block.add(module)
            module2 = scn.Sequential()
            if i < num_strides-1:
                module2.add(
                    scn.BatchNormLeakyReLU(nPlanes[i], leakiness=leakiness)).add(
                    scn.Convolution(self._dimension, nPlanes[i], nPlanes[i+1],
                        downsample[0], downsample[1], False))
            self.encoding_conv.add(module2)
        self.encoding = module

        # Decoding
        self.decoding_conv, self.decoding_blocks = scn.Sequential(), scn.Sequential()
        for i in range(num_strides-2, -1, -1):
            module1 = scn.Sequential().add(
                scn.BatchNormLeakyReLU(nPlanes[i+1], leakiness=leakiness)).add(
                scn.Deconvolution(self._dimension, nPlanes[i+1], nPlanes[i],
                    downsample[0], downsample[1], False))
            self.decoding_conv.add(module1)
            module2 = scn.Sequential()
            for j in range(reps):
                block(module2, nPlanes[i] * (2 if j == 0 else 1), nPlanes[i])
            self.decoding_blocks.add(module2)

        # Clustering convolutions
        if self._N > 0:
            self.clustering_conv = scn.Sequential()
            for i in range(num_strides-2, -1, -1):
                conv = scn.Sequential()
                for _ in range(N):
                    conv.add(scn.SubmanifoldConvolution(self._dimension, nPlanes[i], nPlanes[i], 3, False))
                    conv.add(scn.BatchNormLeakyReLU(nPlanes[i], leakiness=leakiness))
                module = scn.Sequential()
                module.add(scn.ConcatTable()
                                         .add(scn.Identity())
                                         .add(conv))
                module.add(scn.AddTable())
                self.clustering_conv.add(module)

        self.output = scn.Sequential().add(
           scn.BatchNormReLU(m)).add(
           scn.OutputLayer(self._dimension))

        self.linear = torch.nn.Linear(m, num_classes)

    def forward(self, input):
        """
        point_cloud is a list of length minibatch size (assumes mbs = 1)
        point_cloud[0] has 3 spatial coordinates + 1 batch coordinate + 1 feature
        label has shape (point_cloud.shape[0] + 5*num_labels, 1)
        label contains segmentation labels for each point + coords of gt points
        """
        point_cloud, = input
        coords = point_cloud[:, 0:self._dimension+1].float()
        features = point_cloud[:, self._dimension+1:].float()
        x = self.input((coords, features))
        feature_maps = [x]
        feature_ppn = [x]
        for i, layer in enumerate(self.encoding_block):
            x = self.encoding_block[i](x)
            feature_maps.append(x)
            x = self.encoding_conv[i](x)
            feature_ppn.append(x)

        # U-ResNet decoding
        feature_ppn2 = [x]
        for i, layer in enumerate(self.decoding_conv):
            encoding_block = feature_maps[-i-2]
            x = layer(x)
            x = self.concat([encoding_block, x])
            x = self.decoding_blocks[i](x)
            feature_ppn2.append(x)
            if self._N > 0:
                x = self.clustering_conv[i](x)

        x = self.output(x)
        x_seg = self.linear(x)  # Output of UResNet

        return [[x_seg],
                [feature_ppn],
                [feature_ppn2]]


class SegmentationLoss(torch.nn.modules.loss._Loss):
    """
    Loss definition for UResNet.
    Instance clustering flavor
    """
    def __init__(self, cfg, reduction='sum'):
        super(SegmentationLoss, self).__init__(reduction=reduction)
        self._cfg = cfg['modules']['uresnet_clustering']
        self._num_classes = self._cfg.get('num_classes', 5)
        self._depth = self._cfg.get('stride', 5)
        self.cross_entropy = torch.nn.CrossEntropyLoss(reduction='none')

        self._alpha = self._cfg.get('alpha', 1)
        self._beta = self._cfg.get('beta', 1)
        self._gamma = self._cfg.get('gamma', 0.001)
        self._intra_cluster_margin = self._cfg.get('intracluster_margin', 0.5)
        self._inter_cluster_margin = self._cfg.get('intercluster_margin', 1.5)
        self._dimension = self._cfg.get('data_dim', 3)

    def distances(self, v1, v2):
        v1_2 = v1.unsqueeze(1).expand(v1.size(0), v2.size(0), v1.size(1))
        v2_2 = v2.unsqueeze(0).expand(v1.size(0), v2.size(0), v1.size(1))
        return torch.sqrt(torch.pow(v2_2 - v1_2, 2).sum(2) + 0.00001)

    def forward(self, segmentation, label, cluster_label):
        """
        segmentation[0], label and weight are lists of size #gpus = batch_size.
        segmentation has as many elements as UResNet returns.
        label[0] has shape (N, 1) where N is #pts across minibatch_size events.
        """
        assert len(segmentation[0]) == len(label)
        batch_ids = [d[0][:, -2] for d in label]
        uresnet_loss, uresnet_acc = 0., 0.

        cluster_intracluster_loss = 0.
        cluster_intercluster_loss = 0.
        cluster_reg_loss = 0.
        cluster_total_loss = 0.
        cluster_intracluster_loss_per_class = [0.] * self._num_classes
        cluster_intercluster_loss_per_class = [0.] * self._num_classes
        cluster_reg_loss_per_class = [0.] * self._num_classes
        cluster_total_loss_per_class = [0.] * self._num_classes

        for i in range(len(label)):
            max_depth = len(cluster_label[i])
            for b in batch_ids[i].unique():
                batch_index = batch_ids[i] == b

                event_segmentation = segmentation[0][i][batch_index]  # (N, num_classes)
                event_label = label[i][0][batch_index][:, -1][:, None]  # (N, 1)
                event_label = torch.squeeze(event_label, dim=-1).long()

                # Reorder event_segmentation to match event_label
                data_coords = segmentation[2][i][-1].get_spatial_locations()[batch_index][:, :-1]
                perm = np.lexsort((data_coords[:, 2], data_coords[:, 1], data_coords[:, 0]))
                event_segmentation = event_segmentation[perm]

                # Loss for semantic segmentation
                loss_seg = self.cross_entropy(event_segmentation, event_label)
                uresnet_loss += torch.mean(loss_seg)

                # Accuracy for semantic segmentation
                predicted_labels = torch.argmax(event_segmentation, dim=-1)
                acc = (predicted_labels == event_label).sum().item() / float(predicted_labels.nelement())
                uresnet_acc += acc

                # Loss for clustering
                for j, feature_map in enumerate(segmentation[2][i]):
                    if torch.cuda.is_available():
                        batch_index = feature_map.get_spatial_locations()[:, -1].cuda() == b.long()
                    else:
                        batch_index = feature_map.get_spatial_locations()[:, -1] == b.long()
                    hypercoordinates = feature_map.features[batch_index]
                    coordinates = feature_map.get_spatial_locations()[batch_index][:, :-1]
                    clusters = cluster_label[i][-(j+1+(max_depth-self._depth))][cluster_label[i][-(j+1+(max_depth-self._depth))][:, -2] == b]
                    clusters_coordinates = clusters[:, :self._dimension]
                    clusters_labels = clusters[:, -1:]
                    semantic_labels = label[i][-(j+1+(max_depth-self._depth))][label[i][-(j+1+(max_depth-self._depth))][:, -2] == b]

                    # Sort coordinates in lexicographic order
                    x = coordinates.cpu().detach().numpy()
                    perm = np.lexsort((x[:, 2], x[:, 1], x[:, 0]))
                    coordinates = coordinates[perm]

                    # Loop over semantic classes
                    for class_ in range(self._num_classes):
                        class_index = semantic_labels[:, -1] == class_

                        # Identify label clusters
                        clusters_id = clusters_labels[class_index].unique()
                        hyperclusters = []
                        for c in clusters_id:
                            cluster_idx = (clusters_labels[class_index] == c).squeeze()
                            hyperclusters.append(hypercoordinates[class_index][cluster_idx])

                        # Loop over clusters, define intra-cluster loss
                        intra_cluster_loss = 0.
                        means = []
                        zero = torch.tensor(0.)
                        if torch.cuda.is_available(): zero = zero.cuda()
                        C = len(hyperclusters)
                        if C > 0:
                            for cluster in hyperclusters:
                                mean = cluster.mean(dim=0)
                                means.append(mean)
                                intra_cluster_loss += torch.max(((mean - cluster).pow(2).sum(dim=1) + 0.00001).sqrt() - self._intra_cluster_margin, zero).pow(2).mean()
                            intra_cluster_loss /= C
                            means = torch.stack(means)

                        # Define inter-cluster loss
                        inter_cluster_loss = 0.
                        if C > 1:
                            d = torch.max(2 * self._inter_cluster_margin - self.distances(means, means), zero).pow(2)
                            inter_cluster_loss = d[np.triu_indices(d.size(1), k=1)].sum() / (C * (C-1))

                        # Add regularization term
                        reg_loss = 0.
                        if len(means) > 0:
                            reg_loss = (means.pow(2).sum(dim=1) + 0.00001).sqrt().mean()

                        # Compute final loss
                        total_loss = self._alpha * intra_cluster_loss + self._beta * inter_cluster_loss + self._gamma * reg_loss
                        cluster_intracluster_loss += self._alpha * intra_cluster_loss
                        cluster_intercluster_loss += self._beta + inter_cluster_loss
                        cluster_reg_loss += self._gamma * reg_loss
                        cluster_total_loss += total_loss
                        cluster_intracluster_loss_per_class[class_] += self._alpha * intra_cluster_loss
                        cluster_intercluster_loss_per_class[class_] += self._beta + inter_cluster_loss
                        cluster_reg_loss_per_class[class_] += self._gamma * reg_loss
                        cluster_total_loss_per_class[class_] += total_loss

        batch_size = len(batch_ids[i].unique())
        cluster_intracluster_loss /= (batch_size * self._num_classes)
        cluster_intercluster_loss /= (batch_size * self._num_classes)
        cluster_reg_loss /= (batch_size * self._num_classes)
        cluster_total_loss /= (batch_size * self._num_classes)
        cluster_intracluster_loss_per_class = [x/batch_size for x in cluster_intracluster_loss_per_class]
        cluster_intercluster_loss_per_class = [x/batch_size for x in cluster_intercluster_loss_per_class]
        cluster_reg_loss_per_class = [x/batch_size for x in cluster_reg_loss_per_class]
        cluster_total_loss_per_class = [x/batch_size for x in cluster_total_loss_per_class]

        results = {
            'accuracy': uresnet_acc,
            'loss_seg': uresnet_loss + cluster_total_loss,
            'uresnet_loss': uresnet_loss,
            'uresnet_acc': uresnet_acc,
            'intracluster_loss': cluster_intracluster_loss,
            'intercluster_loss': cluster_intercluster_loss,
            'reg_loss': cluster_reg_loss,
            'total_cluster_loss': cluster_total_loss
        }

        for class_ in range(self._num_classes):
            results['intracluster_loss_%d' % class_] = cluster_intracluster_loss_per_class[class_]
            results['intercluster_loss_%d' % class_] = cluster_intercluster_loss_per_class[class_]
            results['reg_loss_%d' % class_] = cluster_reg_loss_per_class[class_]
            results['total_cluster_loss_%d' % class_] = cluster_total_loss_per_class[class_]

        return results

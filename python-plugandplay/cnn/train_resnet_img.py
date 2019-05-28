import os,sys,glob
from skimage.io import imread
sys.path.append(os.path.join(os.getcwd(), "../util"))
from construct_forward_model import construct_forward_model
from sr_util import windowed_sinc, gauss2D, avg_filt
from icdwrapper import Pyicd
import numpy as np
from keras import layers, models
from keras.utils import multi_gpu_model
from keras.models import model_from_json
from keras.optimizers import Adam
import pickle
import matplotlib.pyplot as plt
# allow GPU growth
import tensorflow as tf
from keras.backend.tensorflow_backend import set_session
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
set_session(tf.Session(config=config))

def to_gray(x_in, K):
  [rows_in,cols_in] = np.shape(x_in)[0:2]
  rows_out = rows_in//K*K
  cols_out = cols_in//K*K
  x = np.zeros((rows_out, cols_out))
  for i in range(rows_out):
    for j in range(cols_out):
      r = x_in[i,j,0]
      g = x_in[i,j,1]
      b = x_in[i,j,2]
      x[i,j]=0.2989 * r + 0.5870 * g + 0.1140 * b
  return x

_train = True
sig = 60./255.
sigw = 60./255.
K = 4
h = windowed_sinc(K)
#h = gauss2D((33,33),1)
#h = avg_filt(9)
forward_name = 'sinc'
model_name = 'model_'+forward_name+'_sig60_realim_resnet'

x=[]
y=[]
v=[]
fv=[]

n_samples = 0

for filename in glob.glob('/root/datasets/pmap/*/*.jpg'):
  #print(filename)
  n_samples += 1
  x_in = np.array(imread(filename), dtype=np.float32) / 255.0
  x_img = to_gray(x_in, K)
  v_img = np.random.normal(x_img,sig)
  x.append(x_img)
  v.append(v_img)
  y.append(construct_forward_model(x_img, K, h, sigw))
  fv.append(construct_forward_model(v_img, K, h, 0))

x = np.array(x)
v = np.array(v)
y = np.array(y)
fv = np.array(fv)

# Random Shuffle and training/test set selection
np.random.seed(2019)
n_train = n_samples*8//10
train_idx = np.random.choice(range(0,n_samples), size=n_train, replace=False)
test_idx = list(set(range(0,n_samples))-set(train_idx))
x_train = x[train_idx]
v_train = v[train_idx]
y_train = y[train_idx]
fv_train = fv[train_idx]
gd_train = np.subtract(x_train,v_train)

rows_hr = np.shape(x_train)[1]
cols_hr = np.shape(x_train)[2]
print('rows_hr=',rows_hr)
print('cols_hr=',cols_hr)
in_shp_fv = np.shape(fv_train)[1:]
in_shp_y = np.shape(y_train)[1:]
inshp_v = np.shape(v_train)[1:]
K0 = inshp_v[0]/in_shp_fv[0]
K1 = inshp_v[1]/in_shp_fv[1]

print('fv training data shape: ',np.shape(fv_train))
print('y training data shape: ',np.shape(y_train))

def residual_stack(x, n_chann=8):
  def residual_unit(y,_strides=1):
    shortcut_unit=y
    # 1x1 conv linear
    y = layers.Conv2D(n_chann, (3,3),strides=_strides,padding='same',activation='relu')(y)
    y = layers.BatchNormalization()(y)
    y = layers.Conv2D(n_chann, (3,3),strides=_strides,padding='same',activation='linear')(y)
    y = layers.BatchNormalization()(y)
    # add batch normalization
    y = layers.add([shortcut_unit,y])
    return y

  x = layers.Conv2D(n_chann, (1,1), padding='same',activation='linear')(x)
  x = residual_unit(x)
  x = residual_unit(x)
  # maxpool for down sampling
  return x


### construct neural network graph
input_fv = layers.Input(shape=(rows_hr//K,cols_hr//K))
fv_tf = layers.Reshape(in_shp_fv+(1,))(input_fv)
input_y = layers.Input(shape=(rows_hr//K,cols_hr//K))
y_tf = layers.Reshape(in_shp_y+(1,))(input_y)
input_v = layers.Input(shape=(rows_hr,cols_hr))
v_tf = layers.Reshape(inshp_v+(1,))(input_v)


fv_y=layers.Subtract()([y_tf,fv_tf])

n_channels = 8
fv_y=layers.Conv2D(n_channels,(3,3),activation='relu',padding='same')(fv_y)
v_ = layers.Conv2D(n_channels,(3,3),activation='relu',padding='same')(v_tf)
while (K0 > 1) or (K1 > 1):
  v_ = residual_stack(v_, n_chann=n_channels)
  fv_y = residual_stack(fv_y, n_chann=n_channels)
  fv_y=layers.UpSampling2D((2,2))(fv_y)
  if K0 > 1:
    K0=K0//2
  if K1 > 1:
    K1=K1//2
assert(v_._keras_shape == fv_y._keras_shape)
# merging v and fv-y together
H_merge = layers.concatenate([fv_y, v_],axis=3)
print("Reshaped layer shape is: ",H_merge._keras_shape)
n_channels *= 2
while n_channels >=2 :
  n_channels /= 2
  H_merge = residual_stack(H_merge, n_chann=int(n_channels))
print("H_merge layer shape is: ",H_merge._keras_shape)
H_out = layers.Conv2D(1,(3,3),activation='sigmoid',padding='same')(H_merge)
H_out = layers.Reshape((rows_hr,cols_hr))(H_out)
model = models.Model(inputs=[input_fv,input_y,input_v],output=[H_out])
model = multi_gpu_model(model, gpus=3)
model.summary()


# Start training
batch_size = 256
model.compile(loss='mean_squared_error',optimizer=Adam(lr=0.001))
if _train:
  history = model.fit([fv_train,y_train, v_train], gd_train, epochs=100, batch_size=batch_size,shuffle=True)
  model_json = model.to_json()
  with open(model_name+".json", "w") as json_file:
    json_file.write(model_json)
  model.save_weights(model_name+".h5")
  print("model saved to disk")
  plt.figure()
  plt.plot(np.sqrt(history.history['loss']))
  plt.xlabel('epoch')
  plt.ylabel('loss')
  plt.title('training Loss')
  plt.savefig('loss.png')

# load model 
json_file = open(model_name+'.json', 'r')
loaded_model_json = json_file.read()
json_file.close()
loaded_model = model_from_json(loaded_model_json)
# load weights into model
loaded_model.load_weights(model_name+".h5")
print("Loaded model from disk")

del x_train,y_train,v_train,fv_train

# evaluate test data
x_test = x[test_idx]
v_test = v[test_idx]
y_test = y[test_idx]
fv_test = fv[test_idx]
gd_test = np.subtract(x_test,v_test)
n_test_samples = np.shape(x_test)[0]

loaded_model.compile(loss='mean_squared_error',optimizer='adam')
test_loss = loaded_model.evaluate([fv_test,y_test,v_test], gd_test)
print('test loss:', test_loss)

H_bar = loaded_model.predict([fv_test,y_test,v_test],batch_size=batch_size)
assert(np.shape(H_bar)==np.shape(v_test))
x_hat_test = np.add(H_bar,v_test)

# compare cost of CNN to cost of ICD
print('... evaluating proximal map cost for cnn and icd ...')
icd_cost_avg = 0
cnn_cost_avg = 0
for x_cnn,v_sample,y_sample in zip(x_hat_test[:50],v_test[:50],y_test[:50]):
  # Perform ICD update for current v_sample
  x_cnn = np.reshape(x_cnn,(rows_hr,cols_hr))
  v_sample = np.reshape(v_sample,(rows_hr,cols_hr))
  y_sample = np.reshape(y_sample,(rows_hr//K, cols_hr//K))
  x_icd = np.random.rand(rows_hr,cols_hr)
  icd_cpp = Pyicd(y_sample,h,K,1/(sig*sig),sigw);
  for itr in range(10):
    x_icd = np.array(icd_cpp.update(x_icd,v_sample))
  Gx_icd = construct_forward_model(x_icd,K,h,0)
  Gx_cnn = construct_forward_model(x_cnn,K,h,0)
  icd_cost_avg += sum(sum((Gx_icd-y_sample)**2))/(2*sigw*sigw) + sum(sum((x_icd-v_sample)**2))/(2*sig*sig)
  cnn_cost_avg += sum(sum((Gx_cnn-y_sample)**2))/(2*sigw*sigw) + sum(sum((x_cnn-v_sample)**2))/(2*sig*sig)

icd_cost_avg /= 50
cnn_cost_avg /= 50

print('icd average cost = ',icd_cost_avg)
print('cnn average cost = ',cnn_cost_avg)

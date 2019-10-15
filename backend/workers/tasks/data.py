
from database import (
    fix_ids,
    ImageModel,
    CategoryModel,
    AnnotationModel,
    DatasetModel,
    TaskModel,
    ExportModel
)

# import pycocotools.mask as mask
import numpy as np
import time
import json
import os
import tarfile
import shutil

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFilter

from celery import shared_task
from ..socket import create_socket

def rectangle(draw, xy, fill=255):
    x0, y0, x1, y1 = xy
    draw.rectangle(xy, fill=fill)

def compute_paths(image, export_folder):
    image_path = image['path']
    image_name = image['file_name']
    new_image_path = f"{export_folder}/{'/'.join(image_path.split('/')[2:-1])}/"
    return (image_path, image_name, new_image_path)
    
def save_image(image, directory, image_name):
    if not os.path.exists(directory):
        os.makedirs(directory)
    image.save(f"{directory}{image_name}")

def blur_image(image_path, annotations):
    
    # Open an image and convert to std format
    im = Image.open(image_path).convert('RGB')

    # Create rounded rectangle mask
    mask = Image.new('L', im.size, 0)
    draw = ImageDraw.Draw(mask)
    for annotation in annotations:
        segments = annotation.segmentation
        if not segments:
            continue
        # get the fist segment, asssuming only one bbox
        segment = segments[0]
        # draw mask from this annotations
        rectangle(draw, [segment[0],segment[1],segment[4],segment[5]])
    
    # Blur image
    blurred = im.filter(ImageFilter.GaussianBlur(10))

    # Paste blurred region and save result
    im.paste(blurred, mask=mask)
    return im

@shared_task
def export_annotations(task_id, dataset_id, categories, blur_categories):
    
    task = TaskModel.objects.get(id=task_id)
    dataset = DatasetModel.objects.get(id=dataset_id)

    task.update(status="PROGRESS")
    socket = create_socket()

    task.info("Beginning Export (COCO Format) and Bluring Images")

    db_categories = CategoryModel.objects(id__in=categories, deleted=False) \
        .only(*CategoryModel.COCO_PROPERTIES)
    db_images = ImageModel.objects(deleted=False, annotated=True, dataset_id=dataset.id)\
        .only(*ImageModel.COCO_PROPERTIES)
    db_non_annotated_images = ImageModel.objects(deleted=False, annotated=False, dataset_id=dataset.id)\
        .only(*ImageModel.COCO_PROPERTIES)
    db_annotations = AnnotationModel.objects(deleted=False, category_id__in=categories)
    
    db_blur_annotations = AnnotationModel.objects(deleted=False, category_id__in=blur_categories)
    
    timestamp = time.time()
    directory = f"{dataset.directory}.exports/"
    file_path = f"{directory}coco-{timestamp}"
    file_ext = ".json"

    if not os.path.exists(directory):
        os.makedirs(directory)

    total_items = db_categories.count()
    coco = {
        'images': [],
        'categories': [],
        'annotations': []
    }

    total_items += db_images.count()

    # adding blur items for progress
    total_items += db_blur_annotations.count()

    # adding compression progres points
    total_items += 30

    progress = 0

    # iterate though all categoires and upsert
    category_names = []
    for category in fix_ids(db_categories):

        if len(category.get('keypoint_labels', [])) > 0:
            category['keypoints'] = category.pop('keypoint_labels', [])
            category['skeleton'] = category.pop('keypoint_edges', [])
        else:
            if 'keypoint_edges' in category:
                del category['keypoint_edges']
            if 'keypoint_labels' in category:
                del category['keypoint_labels']

        task.info(f"Adding category: {category.get('name')}")
        coco.get('categories').append(category)
        category_names.append(category.get('name'))
        
        progress += 1
        task.set_progress((progress/total_items)*100, socket=socket)
    
    total_annotations = db_annotations.count()
    total_images = db_images.count()
    for image in fix_ids(db_images):
        image_path, image_name, new_image_path = compute_paths(image, file_path)
        progress += 1
        
        annotations = db_annotations.filter(image_id=image.get('id'))\
            .only(*AnnotationModel.COCO_PROPERTIES)
        blur_annotations = db_blur_annotations.filter(image_id=image.get('id'), isbbox=True)\
            .only(*AnnotationModel.COCO_PROPERTIES)
        annotations = fix_ids(annotations)
        num_annotations = 0
        for annotation in annotations:

            has_keypoints = len(annotation.get('keypoints', [])) > 0
            has_segmentation = len(annotation.get('segmentation', [])) > 0

            if has_keypoints or has_segmentation:

                if not has_keypoints:
                    if 'keypoints' in annotation:
                        del annotation['keypoints']
                else:
                    arr = np.array(annotation.get('keypoints', []))
                    arr = arr[2::3]
                    annotation['num_keypoints'] = len(arr[arr > 0])
                
                num_annotations += 1
                coco.get('annotations').append(annotation)
        task.info(f"Bluring image: {image['file_name']}")
        im = blur_image(image_path, blur_annotations)
        save_image(im, new_image_path, image_name)
        progress += blur_annotations.count()

        task.set_progress((progress/total_items)*100, socket=socket)  


        task.info(f"Exporting {num_annotations} annotations for image {image.get('id')}")
        coco.get('images').append(image)
    
    # save rest of the images
    for image in fix_ids(db_non_annotated_images):
        image_path, image_name, new_image_path = compute_paths(image, file_path)
        save_image(Image.open(image_path).convert('RGB'), new_image_path, image_name)
        
    # compressing blurred images. and removing the actual folder.
    tarball = tarfile.open(f"{file_path}.tar.gz", "w:gz")
    tarball.add(f"{file_path}/", file_path.split('/')[-1])
    tarball.close()
    shutil.rmtree(f"{file_path}/")
    progress += 30

    
    task.set_progress((progress/total_items)*100, socket=socket)  

    task.info(f"Done export {total_annotations} annotations and {total_images} images from {dataset.name}")

    task.info(f"Writing export to file {file_path}")
    with open(file_path+file_ext, 'w') as fp:
        json.dump(coco, fp)

    task.info("Creating export object")
    export = ExportModel(dataset_id=dataset.id, path=file_path+file_ext, tags=["COCO", *category_names])
    export.save()

    task.set_progress(100, socket=socket)


@shared_task
def import_annotations(task_id, dataset_id, coco_json):

    task = TaskModel.objects.get(id=task_id)
    dataset = DatasetModel.objects.get(id=dataset_id)

    task.update(status="PROGRESS")
    socket = create_socket()

    task.info("Beginning Import")

    images = ImageModel.objects(dataset_id=dataset.id)
    categories = CategoryModel.objects

    coco_images = coco_json.get('images', [])
    coco_annotations = coco_json.get('annotations', [])
    coco_categories = coco_json.get('categories', [])

    task.info(f"Importing {len(coco_categories)} categories, "
              f"{len(coco_images)} images, and "
              f"{len(coco_annotations)} annotations")

    total_items = sum([
        len(coco_categories),
        len(coco_annotations),
        len(coco_images)
    ])
    progress = 0

    task.info("===== Importing Categories =====")
    # category id mapping  ( file : database )
    categories_id = {}

    # Create any missing categories
    for category in coco_categories:

        category_name = category.get('name')
        category_id = category.get('id')
        category_model = categories.filter(name__iexact=category_name).first()

        if category_model is None:
            task.warning(f"{category_name} category not found (creating a new one)")
            
            new_category = CategoryModel(
                name=category_name,
                keypoint_edges=category.get('skeleton', []),
                keypoint_labels=category.get('keypoints', [])
            )
            new_category.save()

            category_model = new_category
            dataset.categories.append(new_category.id)

        task.info(f"{category_name} category found")
        # map category ids
        categories_id[category_id] = category_model.id

        # update progress
        progress += 1
        task.set_progress((progress/total_items)*100, socket=socket)

    dataset.update(set__categories=dataset.categories)

    task.info("===== Loading Images =====")
    # image id mapping ( file: database )
    images_id = {}
    categories_by_image = {}

    # Find all images
    for image in coco_images:
        image_id = image.get('id')
        image_filename = image.get('file_name')

        # update progress
        progress += 1
        task.set_progress((progress/total_items)*100, socket=socket)

        image_model = images.filter(file_name__exact=image_filename).all()

        if len(image_model) == 0:
            task.warning(f"Could not find image {image_filename}")
            continue

        if len(image_model) > 1:
            task.error(f"Too many images found with the same file name: {image_filename}")
            continue

        task.info(f"Image {image_filename} found")
        image_model = image_model[0]
        images_id[image_id] = image_model
        categories_by_image[image_id] = list()

    task.info("===== Import Annotations =====")
    for annotation in coco_annotations:

        image_id = annotation.get('image_id')
        category_id = annotation.get('category_id')
        segmentation = annotation.get('segmentation', [])
        keypoints = annotation.get('keypoints', [])
        # is_crowd = annotation.get('iscrowed', False)
        area = annotation.get('area', 0)
        bbox = annotation.get('bbox', [0, 0, 0, 0])
        isbbox = annotation.get('isbbox', False)

        progress += 1
        task.set_progress((progress/total_items)*100, socket=socket)

        has_segmentation = len(segmentation) > 0
        has_keypoints = len(keypoints) > 0
        if not has_segmentation and not has_keypoints:
            task.warning(f"Annotation {annotation.get('id')} has no segmentation or keypoints")
            continue

        try:
            image_model = images_id[image_id]
            category_model_id = categories_id[category_id]
            image_categories = categories_by_image[image_id]
        except KeyError:
            task.warning(f"Could not find image assoicated with annotation {annotation.get('id')}")
            continue

        annotation_model = AnnotationModel.objects(
            image_id=image_model.id,
            category_id=category_model_id,
            segmentation=segmentation,
            keypoints=keypoints
        ).first()

        if annotation_model is None:
            task.info(f"Creating annotation data ({image_id}, {category_id})")

            annotation_model = AnnotationModel(image_id=image_model.id)
            annotation_model.category_id = category_model_id

            annotation_model.color = annotation.get('color')
            annotation_model.metadata = annotation.get('metadata', {})

            if has_segmentation:
                annotation_model.segmentation = segmentation
                annotation_model.area = area
                annotation_model.bbox = bbox
            
            if has_keypoints:
                annotation_model.keypoints = keypoints

            annotation_model.isbbox = isbbox
            annotation_model.save()

            image_categories.append(category_id)
        else:
            annotation_model.update(deleted=False, isbbox=isbbox)
            task.info(f"Annotation already exists (i:{image_id}, c:{category_id})")

    for image_id in images_id:
        
        image_model = images_id[image_id]
        category_ids = categories_by_image[image_id]
        all_category_ids = list(image_model.category_ids)
        all_category_ids += category_ids

        image_model.update(
            set__annotated=True,
            set__category_ids=list(set(all_category_ids)),
            set__num_annotations=AnnotationModel\
                .objects(image_id=image_id, area__gt=0, deleted=False).count()
        )

    task.set_progress(100, socket=socket)


__all__ = ["export_annotations", "import_annotations"]